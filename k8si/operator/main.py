"""Kopf operator: reconciles K8siBackup CRDs and runs snapshot-first backup pipeline."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import kopf
import kubernetes
import kubernetes.client
from croniter import croniter

from . import metrics, workflow
from .cronjob import K8SI_IMAGE, build_restore_patch
from .status import compute_next_backup

log = logging.getLogger(__name__)

_running: set[tuple[str, str]] = set()


def _daily_failure_count(status: dict) -> int:
    today = datetime.now(tz=UTC).date().isoformat()
    return sum(
        1
        for entry in status.get("recentBackups", [])
        if entry.get("result") == "failed" and entry.get("time", "").startswith(today)
    )


def _is_manual_trigger(triggered_at: str | None, last_backup_time: str | None) -> bool:
    """Return True if triggeredAt is set and newer than lastBackupTime."""
    if not triggered_at:
        return False
    try:
        triggered = datetime.fromisoformat(triggered_at)
    except ValueError:
        return False
    if last_backup_time is None:
        return True
    try:
        last = datetime.fromisoformat(last_backup_time)
    except ValueError:
        return True
    return triggered > last


def _is_in_window(window: dict, now: datetime | None = None) -> bool:
    """Return True if now (UTC) falls within [start, end).

    If start > end the window wraps midnight. An absent or invalid window
    means backups are always allowed.
    """
    if not window:
        return True
    now_dt = now if now is not None else datetime.now(tz=UTC)
    try:
        sh, sm = map(int, window.get("start", "00:00").split(":"))
        eh, em = map(int, window.get("end", "23:59").split(":"))
    except (ValueError, TypeError):
        return True
    now_m = now_dt.hour * 60 + now_dt.minute
    s = sh * 60 + sm
    e = eh * 60 + em
    if s <= e:
        return s <= now_m < e
    return now_m >= s or now_m < e


async def _notify_webhook(url: str, payload: dict[str, Any]) -> None:
    """POST a JSON payload to a webhook URL; silently swallows all errors."""
    try:
        await asyncio.to_thread(httpx.post, url, json=payload, timeout=10.0)
    except Exception as e:
        log.warning("Webhook to %s failed: %s", url, e)


def _is_due(schedule: str, last_backup_time: str | None) -> bool:
    now = datetime.now(tz=UTC)
    if last_backup_time is None:
        return True
    try:
        last = datetime.fromisoformat(last_backup_time)
    except ValueError:
        return True
    cron = croniter(schedule, last)
    return bool(now >= cron.get_next(datetime))  # type: ignore[arg-type]


@kopf.on.login()  # type: ignore[arg-type]
def login(**kwargs: object) -> kopf.ConnectionInfo:
    return kopf.login_with_service_account(**kwargs)  # type: ignore[return-value, arg-type]


@kopf.on.startup()  # type: ignore[arg-type]
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
            name,
            namespace,
            status.get("lastBackupResult", ""),
            status.get("lastBackupTime"),
        )


# ── CRD lifecycle ──────────────────────────────────────────────────────────────


@kopf.on.create("k8si.io", "v1", "k8sibackups")  # type: ignore[arg-type]
def on_create(
    body: dict,
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
    kopf.event(body, type="Normal", reason="Registered", message=f"K8siBackup {name} registered")


@kopf.on.update("k8si.io", "v1", "k8sibackups")  # type: ignore[arg-type]
def on_update(
    body: dict,
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
    kopf.event(body, type="Normal", reason="Updated", message=f"K8siBackup {name} updated")


@kopf.on.delete("k8si.io", "v1", "k8sibackups")  # type: ignore[arg-type]
def on_delete(name: str, namespace: str, logger: logging.Logger, **_: object) -> None:
    _running.discard((namespace, name))
    metrics.remove(name, namespace)
    logger.info("K8siBackup %s/%s deleted", namespace, name)


# ── Backup timer ───────────────────────────────────────────────────────────────


@kopf.timer("k8si.io", "v1", "k8sibackups", interval=60.0, idle=60.0)  # type: ignore[arg-type]
async def backup_timer(
    body: dict,
    spec: dict,
    name: str,
    namespace: str,
    status: dict,
    patch: kopf.Patch,
    logger: logging.Logger,
    **_: object,
) -> None:
    if spec.get("paused", False):
        logger.info("Backup %s/%s paused, skipping", namespace, name)
        return

    last_backup = status.get("lastBackupTime")
    is_triggered = _is_manual_trigger(status.get("triggeredAt"), last_backup)
    schedule = spec["schedule"]

    if not is_triggered:
        window = spec.get("backupWindow", {})
        if window and not _is_in_window(window):
            logger.debug("Backup %s/%s outside backup window, skipping", namespace, name)
            return
        if not _is_due(schedule, last_backup):
            return

    key = (namespace, name)
    if key in _running:
        logger.warning("Backup %s/%s still running, skipping", namespace, name)
        return

    if not is_triggered:
        max_retries = spec.get("maxRetriesPerDay", 3)
        failures_today = _daily_failure_count(status)
        if failures_today >= max_retries:
            logger.warning(
                "Backup %s/%s skipped: %d failures today (maxRetriesPerDay=%d)",
                namespace,
                name,
                failures_today,
                max_retries,
            )
            return

    _running.add(key)
    patch.status["lastBackupResult"] = "running"
    if is_triggered:
        patch.status["triggeredAt"] = None
        logger.info("Backup %s/%s triggered manually, clearing triggeredAt", namespace, name)
    metrics.record(name, namespace, "running", last_backup)
    kopf.event(body, type="Normal", reason="BackupStarted", message=f"Backup started for {name}")
    backup_start = datetime.now(tz=UTC)
    try:
        result = await workflow.run_backup(name, namespace, spec, logger, body)
        duration = int((datetime.now(tz=UTC) - backup_start).total_seconds())
        patch.status.update(result)
        patch.status["lastBackupDuration"] = duration
        patch.status["nextBackupTime"] = compute_next_backup(schedule)
        now_iso = result.get("lastBackupTime") or datetime.now(tz=UTC).isoformat()
        recent = list(status.get("recentBackups", []))
        recent.insert(0, {"time": now_iso, "result": "success"})
        patch.status["recentBackups"] = recent[:30]
        metrics.record(name, namespace, "success", result.get("lastBackupTime"), duration=duration)
        kopf.event(body, type="Normal", reason="BackupSucceeded", message=f"Backup done: {name}")
        webhook = spec.get("notifyOnSuccess")
        if webhook:
            await _notify_webhook(
                webhook,
                {
                    "name": name,
                    "namespace": namespace,
                    "result": "success",
                    "message": result.get("message", ""),
                    "time": now_iso,
                    "duration": duration,
                },
            )
    except Exception as e:
        duration = int((datetime.now(tz=UTC) - backup_start).total_seconds())
        logger.error("Backup %s/%s failed: %s", namespace, name, e)
        patch.status["lastBackupResult"] = "failed"
        patch.status["lastBackupDuration"] = duration
        patch.status["message"] = str(e)
        now_iso = datetime.now(tz=UTC).isoformat()
        recent = list(status.get("recentBackups", []))
        recent.insert(0, {"time": now_iso, "result": "failed"})
        patch.status["recentBackups"] = recent[:30]
        metrics.record(name, namespace, "failed", last_backup, duration=duration)
        kopf.event(body, type="Warning", reason="BackupFailed", message=f"PVC backup failed: {e}")
        webhook = spec.get("notifyOnFailure")
        if webhook:
            await _notify_webhook(
                webhook,
                {
                    "name": name,
                    "namespace": namespace,
                    "result": "failed",
                    "message": str(e),
                    "time": now_iso,
                    "duration": duration,
                },
            )
    finally:
        _running.discard(key)
