"""Sidecar mode: periodic backup with retention."""

import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from croniter import croniter

from .backend import BackupBackend, BackupError
from .config import Config

log = logging.getLogger(__name__)

LAST_BACKUP_FILE = ".k8si-last-backup"


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

    cron = croniter(config.backup_schedule, datetime.now(tz=timezone.utc))

    while True:
        next_run: datetime = cron.get_next(datetime)  # type: ignore[assignment]
        delay = (next_run - datetime.now(tz=timezone.utc)).total_seconds()
        log.info("Next backup at %s (in %.0fs)", next_run.isoformat(), delay)
        time.sleep(max(0, delay))

        _run_cycle(config, backend)


def _run_cycle(config: Config, backend: BackupBackend) -> None:
    if config.pre_backup_hook:
        _run_hook(config.pre_backup_hook)

    try:
        backend.backup(source=config.data_path, tags=config.backup_tags)
    except BackupError as e:
        if "repository does not exist" in e.stderr:
            log.info("Repository not initialised, running init")
            try:
                backend.init()
                backend.backup(source=config.data_path, tags=config.backup_tags)
            except BackupError as init_err:
                log.error("Backup failed after init: %s", init_err.stderr)
                return
        else:
            log.error("Backup failed (will retry next cycle): %s", e.stderr)
            return

    try:
        backend.forget(
            daily=config.retention_daily,
            weekly=config.retention_weekly,
            monthly=config.retention_monthly,
            prune=True,
        )
    except BackupError as e:
        log.error("Forget/prune failed: %s", e.stderr)
        return

    _write_last_backup_timestamp(config.data_path)
    log.info("PVC backup complete.")


def _run_hook(hook: Path) -> None:
    log.info("Running pre-backup hook: %s", hook)
    result = subprocess.run([str(hook)], capture_output=True, text=True)
    if result.stdout:
        log.info("hook stdout: %s", result.stdout.strip())
    if result.returncode != 0:
        log.error("Pre-backup hook failed (exit %d): %s", result.returncode, result.stderr.strip())


def _write_last_backup_timestamp(data_path: Path) -> None:
    ts_file = data_path / LAST_BACKUP_FILE
    try:
        ts_file.write_text(datetime.now(tz=timezone.utc).isoformat())
    except OSError as e:
        log.warning("Could not write last-backup timestamp: %s", e)
