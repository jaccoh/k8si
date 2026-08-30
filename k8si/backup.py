"""Sidecar mode: periodic backup with retention."""

import json
import logging
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from croniter import croniter

from .backend import (
    BackupBackend,
    BackupError,
    RepositoryLockedError,
    RepositoryNotInitializedError,
)
from .config import Config

log = logging.getLogger(__name__)

ARTIFACT_MARKER = "K8SI_ARTIFACT "


def run_once(config: Config, backend: BackupBackend) -> None:
    """Single backup cycle — used by the operator CronJob (MODE=job)."""
    log.info("PVC backup job starting. Repo: %s", config.restic_repository)
    _run_cycle(config, backend)


def run(config: Config, backend: BackupBackend) -> None:
    assert config.backup_schedule is not None
    log.info(
        "PVC backup sidecar starting. Schedule: %s, repo: %s",
        config.backup_schedule,
        config.restic_repository,
    )

    cron = croniter(config.backup_schedule, datetime.now(tz=UTC))

    while True:
        next_run: datetime = cron.get_next(datetime)  # type: ignore[assignment]
        delay = (next_run - datetime.now(tz=UTC)).total_seconds()
        log.info("Next backup at %s (in %.0fs)", next_run.isoformat(), delay)
        time.sleep(max(0, delay))

        _run_cycle(config, backend)


def _run_cycle(config: Config, backend: BackupBackend) -> None:
    try:
        backend.unlock()
    except Exception as e:
        log.warning("Proactive unlock failed (continuing): %s", e)

    if config.pre_snapshot_hook:
        _run_hook(config.pre_snapshot_hook, required=config.pre_snapshot_hook_required)

    try:
        backend.backup(source=config.data_path, tags=config.backup_tags)
    except BackupError as e:
        if isinstance(e, RepositoryNotInitializedError) or "repository does not exist" in e.stderr:
            log.info("Repository not initialised, running init")
            try:
                backend.init()
                backend.backup(source=config.data_path, tags=config.backup_tags)
            except BackupError as init_err:
                log.error("Backup failed after init: %s", init_err.stderr)
                raise
        elif isinstance(e, RepositoryLockedError):
            log.warning("Repository is locked, attempting automated unlock and retry")
            try:
                backend.unlock()
                backend.backup(source=config.data_path, tags=config.backup_tags)
            except BackupError as retry_err:
                log.error("Backup failed after unlock retry: %s", retry_err.stderr)
                raise
        else:
            log.error("Backup failed: %s", e.stderr)
            raise

    _emit_artifact(config, backend)

    try:
        backend.forget(
            daily=config.retention_daily,
            weekly=config.retention_weekly,
            monthly=config.retention_monthly,
            prune=True,
        )
    except RepositoryLockedError:
        log.warning("Repository is locked during forget, attempting automated unlock and retry")
        try:
            backend.unlock()
            backend.forget(
                daily=config.retention_daily,
                weekly=config.retention_weekly,
                monthly=config.retention_monthly,
                prune=True,
            )
        except BackupError as retry_err:
            log.error("Forget failed after unlock retry: %s", retry_err.stderr)
            raise
    except BackupError as e:
        log.error("Forget/prune failed: %s", e.stderr)
        raise

    if config.run_check:
        try:
            backend.check()
            log.info("Repository integrity check passed.")
        except BackupError as e:
            log.error("Repository check failed: %s", e.stderr)

    log.info("PVC backup complete.")


def _run_hook(hook: Path, *, required: bool = False) -> None:
    log.info("Running pre-snapshot hook: %s", hook)
    result = subprocess.run([str(hook)], capture_output=True, text=True)
    if result.stdout:
        log.info("hook stdout: %s", result.stdout.strip())
    if result.returncode != 0:
        msg = f"Pre-snapshot hook failed (exit {result.returncode}): {result.stderr.strip()}"
        if required:
            raise RuntimeError(msg)
        log.error(msg)


def _emit_artifact(config: Config, backend: BackupBackend) -> None:
    """Resolve the backup artifact via the backend's metadata API and print one
    structured line (``K8SI_ARTIFACT {...}``) for the operator to parse.

    Structured output beats the operator's log-scraping fallback: restic and
    kopia human-readable formats drift between versions, while
    ``restic snapshots --json`` and kopia's create manifest do not.
    Best-effort — a resolution failure never fails the backup.
    """
    artifact = _resolve_artifact(config, backend)
    if artifact:
        print(f"{ARTIFACT_MARKER}{json.dumps(artifact)}", flush=True)


def _resolve_artifact(config: Config, backend: BackupBackend) -> dict | None:
    """Return {"snapshotId": ..., "sizeBytes": ...} for the just-made snapshot."""
    # Exact source first: the manifest kopia printed during `snapshot create --json`.
    captured = getattr(backend, "last_snapshot", None)
    if isinstance(captured, dict) and captured.get("snapshotId"):
        return {"snapshotId": captured["snapshotId"], "sizeBytes": captured.get("sizeBytes")}
    try:
        snaps = backend.snapshots(tags=config.backup_tags)
        if not snaps:
            return None
        latest = max(snaps, key=lambda s: str(s.get("time", "")))
        snap_id = str(latest.get("id") or "")
        if not snap_id:
            return None
        return {"snapshotId": snap_id, "sizeBytes": backend.snapshot_size(snap_id)}
    except Exception as e:
        log.warning("Artifact resolution via backend metadata failed: %s", e)
        return None
