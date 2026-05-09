"""Kopf operator: reconciles K8siBackup CRDs and runs snapshot-first backup pipeline."""

import logging

import kopf
import kubernetes
import kubernetes.client

from .cronjob import K8SI_IMAGE, build_restore_patch
from .status import compute_next_backup
from . import workflow
from croniter import croniter
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_running: set[tuple[str, str]] = set()


def _is_due(schedule: str, last_backup_time: str | None) -> bool:
    now = datetime.now(tz=timezone.utc)
    if last_backup_time is None:
        return True
    try:
        last = datetime.fromisoformat(last_backup_time)
    except ValueError:
        return True
    cron = croniter(schedule, last)
    return now >= cron.get_next(datetime)  # type: ignore[arg-type]


@kopf.on.startup()
def startup(logger: logging.Logger, **_: object) -> None:
    kubernetes.config.load_incluster_config()
    logger.info("k8si operator started, image=%s", K8SI_IMAGE)


# ── CRD lifecycle ──────────────────────────────────────────────────────────────

@kopf.on.create("k8si.io", "v1", "k8sibackups")
def on_create(
    spec: dict,
    name: str,
    namespace: str,
    patch: kopf.Patch,
    logger: logging.Logger,
    **_: object,
) -> None:
    patch.status["lastBackupResult"] = "pending"
    patch.status["nextBackupTime"] = compute_next_backup(spec["schedule"])
    patch.status["restorePatch"] = build_restore_patch(spec)
    logger.info("K8siBackup %s/%s registered", namespace, name)


@kopf.on.update("k8si.io", "v1", "k8sibackups")
def on_update(
    spec: dict,
    name: str,
    namespace: str,
    patch: kopf.Patch,
    logger: logging.Logger,
    **_: object,
) -> None:
    patch.status["nextBackupTime"] = compute_next_backup(spec["schedule"])
    patch.status["restorePatch"] = build_restore_patch(spec)
    logger.info("K8siBackup %s/%s updated", namespace, name)


@kopf.on.delete("k8si.io", "v1", "k8sibackups")
def on_delete(name: str, namespace: str, logger: logging.Logger, **_: object) -> None:
    _running.discard((namespace, name))
    logger.info("K8siBackup %s/%s deleted", namespace, name)


# ── Backup timer ───────────────────────────────────────────────────────────────

@kopf.timer("k8si.io", "v1", "k8sibackups", interval=60.0, idle=60.0)
async def backup_timer(
    spec: dict,
    name: str,
    namespace: str,
    status: dict,
    patch: kopf.Patch,
    logger: logging.Logger,
    **_: object,
) -> None:
    schedule = spec["schedule"]
    last_backup = status.get("lastBackupTime")

    if not _is_due(schedule, last_backup):
        return

    key = (namespace, name)
    if key in _running:
        logger.warning("Backup %s/%s still running, skipping", namespace, name)
        return

    _running.add(key)
    patch.status["lastBackupResult"] = "running"
    try:
        result = await workflow.run_backup(name, namespace, spec, logger)
        patch.status.update(result)
        patch.status["nextBackupTime"] = compute_next_backup(schedule)
    except Exception as e:
        logger.error("Backup %s/%s failed: %s", namespace, name, e)
        patch.status["lastBackupResult"] = "failed"
        patch.status["message"] = str(e)
    finally:
        _running.discard(key)
