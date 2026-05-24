"""Kopf operator: reconciles K8siBackup CRDs and runs snapshot-first backup pipeline."""

import logging
from datetime import UTC, datetime

import kopf
import kubernetes
import kubernetes.client
from croniter import croniter

from . import metrics, workflow
from .cronjob import K8SI_IMAGE, build_restore_patch
from .status import compute_next_backup

log = logging.getLogger(__name__)

_running: set[tuple[str, str]] = set()


def _is_due(schedule: str, last_backup_time: str | None) -> bool:
    now = datetime.now(tz=UTC)
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
    metrics.start()
    _init_metrics(logger)
    logger.info("k8si operator started, image=%s", K8SI_IMAGE)


def _init_metrics(logger: logging.Logger) -> None:
    """Seed metrics from existing K8siBackup statuses so gauges aren't empty after restart."""
    custom = kubernetes.client.CustomObjectsApi()
    try:
        items = custom.list_cluster_custom_object("k8si.io", "v1", "k8sibackups").get("items", [])
    except Exception as e:
        logger.warning("Could not list K8siBackups for metrics init: %s", e)
        return
    for obj in items:
        name = obj["metadata"]["name"]
        namespace = obj["metadata"]["namespace"]
        status = obj.get("status", {})
        metrics.record(
            name, namespace,
            status.get("lastBackupResult", ""), status.get("lastBackupTime"),
        )


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
    metrics.remove(name, namespace)
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
    metrics.record(name, namespace, "running", last_backup)
    try:
        result = await workflow.run_backup(name, namespace, spec, logger)
        patch.status.update(result)
        patch.status["nextBackupTime"] = compute_next_backup(schedule)
        metrics.record(name, namespace, "success", result.get("lastBackupTime"))
    except Exception as e:
        logger.error("Backup %s/%s failed: %s", namespace, name, e)
        patch.status["lastBackupResult"] = "failed"
        patch.status["message"] = str(e)
        metrics.record(name, namespace, "failed", last_backup)
    finally:
        _running.discard(key)
