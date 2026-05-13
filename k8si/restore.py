"""Init container mode: restore from restic with pre-restore sanity checks."""

import fcntl
import logging
from datetime import datetime, timezone
from pathlib import Path

from .backend import BackupBackend, BackupError, NoSnapshotsError
from .config import Config

log = logging.getLogger(__name__)

MARKER_FILE = ".k8si-restore-complete"
NO_RESTORE_FILE = ".k8si-no-restore"
LOCK_FILE = ".k8si-restore.lock"


def run(config: Config, backend: BackupBackend) -> None:
    data_path = config.data_path

    # Emergency override: file on the volume (survives ArgoCD, no git commit needed)
    if (data_path / NO_RESTORE_FILE).exists():
        log.info("Restore disabled by %s on volume, skipping", NO_RESTORE_FILE)
        return

    sentinels = [data_path / s for s in config.restore_sentinels]
    marker = data_path / MARKER_FILE

    # Step 1: all sentinels on disk → data is healthy, skip
    if sentinels and all(s.exists() for s in sentinels):
        log.info(
            "PVC healthy, skipping restore (sentinels present: %s)",
            [s.name for s in sentinels],
        )
        return

    # Step 2: marker exists but sentinels missing → post-restore corruption
    if marker.exists() and sentinels:
        missing = [s.name for s in sentinels if not s.exists()]
        log.error(
            "PVC corruption detected: restore marker exists but sentinels are missing: %s",
            missing,
        )
        raise SystemExit(1)

    # Step 3: no sentinels configured, use marker alone
    if not sentinels and marker.exists():
        log.info("PVC already initialized (marker present), skipping restore")
        return

    # Snapshot override annotation: skip remote checks, go straight to restore
    snapshot_id = config.restore_snapshot
    if snapshot_id:
        log.info("Using pinned snapshot: %s", snapshot_id)
        if config.restore_sentinels and not _sentinels_in_snapshot(backend, snapshot_id, config.restore_sentinels):
            log.error("Pinned snapshot %s is missing required sentinels, aborting", snapshot_id)
            raise SystemExit(1)
    else:
        snapshot_id = _pick_snapshot(config, backend)
        if snapshot_id is None:
            return

    _do_restore(backend, snapshot_id, data_path, sentinels, marker)


def _pick_snapshot(config: Config, backend: BackupBackend) -> str | None:
    """Run all pre-restore checks and return an eligible snapshot ID, or None to skip."""
    try:
        snapshots = backend.snapshots(tags=config.restore_tags or None)
    except BackupError as e:
        log.error("Cannot reach backup repository: %s", e.stderr or e)
        raise SystemExit(1) from e

    if not snapshots:
        if config.restore_required:
            log.error(
                "No snapshots found (tags=%s) and restore.required=true — failing",
                config.restore_tags,
            )
            raise SystemExit(1)
        log.info(
            "No snapshots found (tags=%s) — assuming fresh PVC, skipping restore",
            config.restore_tags,
        )
        return None

    latest = snapshots[-1]
    snapshot_id: str = latest.get("short_id") or latest["id"][:8]
    snap_time_str: str = latest["time"]

    # Age check (opt-in)
    if config.restore_max_age_hours is not None:
        snap_time = datetime.fromisoformat(snap_time_str.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - snap_time).total_seconds() / 3600
        if age_hours > config.restore_max_age_hours:
            log.warning(
                "Skipping restore: latest snapshot %s is %.1fh old (max %.0fh)",
                snapshot_id, age_hours, config.restore_max_age_hours,
            )
            return None
        log.info("Snapshot %s age: %.1fh (within %.0fh limit)", snapshot_id, age_hours,
                 config.restore_max_age_hours)

    # Size check (opt-in)
    if config.restore_size_min is not None or config.restore_size_max is not None:
        size = backend.snapshot_size(snapshot_id)
        if config.restore_size_min is not None and size < config.restore_size_min:
            log.warning(
                "Skipping restore: snapshot %s is %d bytes (min %d)",
                snapshot_id, size, config.restore_size_min,
            )
            return None
        if config.restore_size_max is not None and size > config.restore_size_max:
            log.warning(
                "Skipping restore: snapshot %s is %d bytes (max %d)",
                snapshot_id, size, config.restore_size_max,
            )
            return None
        log.info("Snapshot %s size: %d bytes (within bounds)", snapshot_id, size)

    # Sentinel quality gate: verify sentinels are present in the snapshot
    if config.restore_sentinels:
        if not _sentinels_in_snapshot(backend, snapshot_id, config.restore_sentinels):
            return None

    return snapshot_id


def _sentinels_in_snapshot(
    backend: BackupBackend, snapshot_id: str, sentinels: list[str]
) -> bool:
    if not sentinels:
        return True
    log.info("Checking sentinels in snapshot %s: %s", snapshot_id, sentinels)
    found = backend.check_sentinels(snapshot_id, sentinels)
    if found:
        log.info("All sentinels confirmed in snapshot %s", snapshot_id)
    return found


def _do_restore(
    backend: BackupBackend,
    snapshot_id: str,
    data_path: Path,
    sentinels: list[Path],
    marker: Path,
) -> None:
    lock_path = data_path / LOCK_FILE
    lock_file = lock_path.open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("Another restore is in progress (lock held), skipping")
        lock_file.close()
        return

    try:
        log.info("Restoring PVC from snapshot %s", snapshot_id)
        try:
            backend.restore(snapshot_id=snapshot_id)
        except BackupError as e:
            log.error("PVC restore failed: %s", e.stderr)
            raise SystemExit(1) from e

        # Post-restore: verify sentinels are now present
        if sentinels:
            missing = [s.name for s in sentinels if not s.exists()]
            if missing:
                log.error(
                    "PVC restore completed but sentinels still missing: %s — volume may be corrupt",
                    missing,
                )
                raise SystemExit(1)
            log.info("Post-restore sentinel check passed: %s", [s.name for s in sentinels])

        marker.write_text("restored\n")
        log.info("PVC restore complete")

    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        lock_path.unlink(missing_ok=True)
