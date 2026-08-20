"""Kopf operator: reconciles K8siBackup CRDs and runs snapshot-first backup pipeline."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx2 as httpx
import kopf
import kubernetes
import kubernetes.client
from croniter import croniter

from . import metrics, workflow
from .cronjob import K8SI_IMAGE, build_restore_patch
from .status import compute_next_backup
from .workflow import _patch_run_status

log = logging.getLogger(__name__)

# Process-local guard against concurrent runs of the same backup.
# Populated and owned by on_run_create; backup_timer only reads it.
_running: set[tuple[str, str]] = set()


def _has_active_run_sync(namespace: str, backup_name: str) -> bool:
    """Return True if any K8siBackupRun for this backup is Pending or Running in K8s.

    Used by backup_timer to guard against creating a duplicate run after an operator
    restart (when the in-memory _running set is empty).
    """
    try:
        custom = kubernetes.client.CustomObjectsApi()
        runs = custom.list_namespaced_custom_object(
            "k8si.io",
            "v1",
            namespace,
            "k8sibackupruns",
            label_selector=f"k8si.io/backup={backup_name}",
        )
        for run in runs.get("items", []):
            if run.get("status", {}).get("phase", "Pending") in ("Pending", "Running"):
                return True
    except Exception:
        pass
    return False


def _check_prerequisites(logger: logging.Logger) -> None:
    """Check that required CRDs and RBAC are in place; log errors if not."""
    custom = kubernetes.client.CustomObjectsApi()
    try:
        custom.list_cluster_custom_object("k8si.io", "v1", "k8sibackupruns")
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            logger.error(
                "HEALTH: k8sibackupruns CRD missing — apply: kubectl apply -f deploy/crd_run.yaml"
            )
        elif e.status == 403:
            logger.error("HEALTH: operator lacks RBAC for k8sibackupruns — check: deploy/rbac.yaml")
        else:
            logger.warning("HEALTH: k8sibackupruns check failed: %s", e)
    except Exception as e:
        logger.warning("HEALTH: k8sibackupruns check failed: %s", e)


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


def _webhook_url_allowed(url: str) -> bool:
    """Reject webhook URLs that point back into the operator's own network.

    notifyOnSuccess/notifyOnFailure are free-text CRD fields, and a patched
    spec would otherwise make the operator POST into the cluster (cloud
    metadata, kube-apiserver, internal services — goals #3). Blocks non-http(s)
    schemes and literal private/loopback/link-local targets. Hostname-based
    SSRF (a public name resolving internally) needs a NetworkPolicy; this is
    the address-literal gate.
    """
    import ipaddress
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return True  # a hostname, not an address literal
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


async def _notify_webhook(url: str, payload: dict[str, Any]) -> None:
    """POST a JSON payload to a webhook URL; silently swallows all errors."""
    if not _webhook_url_allowed(url):
        log.warning("Webhook URL %s refused by SSRF guard — not delivered", url)
        return
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
    _check_prerequisites(logger)
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


# ── K8siBackup lifecycle ──────────────────────────────────────────────────────


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

    if await asyncio.to_thread(_has_active_run_sync, namespace, name):
        logger.warning("Backup %s/%s has active K8siBackupRun in K8s, skipping", namespace, name)
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

    ts = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S")
    run_name = f"{name}-{ts}"
    triggered_by = "manual" if is_triggered else "schedule"
    triggered_at = datetime.now(tz=UTC).isoformat()
    mode = spec.get("backupMode", "snapshot")

    if is_triggered:
        patch.status["triggeredAt"] = None
        logger.info("Backup %s/%s triggered manually, clearing triggeredAt", namespace, name)

    run_obj = {
        "apiVersion": "k8si.io/v1",
        "kind": "K8siBackupRun",
        "metadata": {
            "name": run_name,
            "namespace": namespace,
            "labels": {"k8si.io/backup": name},
            "ownerReferences": [
                {
                    "apiVersion": "k8si.io/v1",
                    "kind": "K8siBackup",
                    "name": name,
                    "uid": body["metadata"]["uid"],
                    "controller": True,
                    "blockOwnerDeletion": False,
                }
            ],
        },
        "spec": {
            "backupRef": name,
            "triggeredBy": triggered_by,
            "triggeredAt": triggered_at,
            "mode": mode,
        },
    }

    custom = kubernetes.client.CustomObjectsApi()
    try:
        await asyncio.to_thread(
            custom.create_namespaced_custom_object,
            "k8si.io",
            "v1",
            namespace,
            "k8sibackupruns",
            run_obj,
        )
        logger.info("Created K8siBackupRun %s/%s", namespace, run_name)
        patch.status["lastBackupResult"] = "running"
        patch.status["lastRunRef"] = run_name
        metrics.record(name, namespace, "running", last_backup)
        kopf.event(body, type="Normal", reason="BackupStarted", message=f"Created run {run_name}")
    except Exception as e:
        logger.error("Failed to create K8siBackupRun %s/%s: %s", namespace, run_name, e)


# ── K8siBackupRun reconciler ──────────────────────────────────────────────────


def _stuck_run_threshold_min(job: object) -> float:
    """Minutes a Running run may age before the reconciler fails it.

    Default 60; when the Job is visible, its own activeDeadlineSeconds (+5 min
    grace) is the budget — a backup with jobTimeout > 1h used to be failed at
    the hardcoded hour mark while its Job was still legitimately running (#5).
    """
    deadline = getattr(getattr(job, "spec", None), "active_deadline_seconds", None)
    if isinstance(deadline, (int, float)):
        return max(60.0, deadline / 60 + 5)
    return 60.0


@kopf.timer("k8si.io", "v1", "k8sibackupruns", interval=60.0)  # type: ignore[arg-type]
async def run_reconcile_timer(
    body: dict,
    name: str,
    namespace: str,
    status: dict,
    logger: logging.Logger,
    **_: object,
) -> None:
    """Mark orphaned K8siBackupRuns as Failed so the backup timer can retry."""
    phase = status.get("phase", "Pending")
    if phase in ("Succeeded", "Failed"):
        return

    now = datetime.now(tz=UTC)
    meta = body.get("metadata", {})
    message: str | None = None

    if phase == "Pending":
        # Fast path: cached status already has log entries — workflow is running.
        if status.get("log"):
            return
        # Kopf's cache can be stale right after an operator restart.  Re-read from
        # the API before deciding to kill so we don't falsely terminate a live run.
        try:
            custom = kubernetes.client.CustomObjectsApi()
            live = await asyncio.to_thread(
                custom.get_namespaced_custom_object,
                "k8si.io",
                "v1",
                namespace,
                "k8sibackupruns",
                name,
            )
            live_status = live.get("status", {})
            if live_status.get("phase") in ("Succeeded", "Failed"):
                return  # already terminal in the API
            if live_status.get("log"):
                logger.info(
                    "K8siBackupRun %s/%s Pending in cache but log entries found in API"
                    " — backup is executing, skipping kill",
                    namespace,
                    name,
                )
                return
        except Exception as exc:
            logger.warning(
                "run_reconcile_timer: could not re-read %s/%s: %s — proceeding with kill",
                namespace,
                name,
                exc,
            )
        created_str = meta.get("creationTimestamp", "")
        if created_str:
            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            age_min = (now - created).total_seconds() / 60
            if age_min >= 5:
                logger.warning(
                    "K8siBackupRun %s/%s stuck Pending for %.0f min, marking Failed",
                    namespace,
                    name,
                    age_min,
                )
                message = (
                    f"stuck in Pending for {age_min:.0f}m"
                    " — operator may have missed the create event"
                )
    elif phase == "Running":
        start_str = status.get("startTime", "")
        age_min = 0.0
        if start_str:
            start = datetime.fromisoformat(start_str)
            age_min = (now - start).total_seconds() / 60

        # Jobs are named k8si-{backup}-{ts}, never the run name — use the
        # jobName recorded on the run status (#5).
        job_name = str(status.get("jobName") or name)

        # Check the K8s Job — it may have finished while run status wasn't updated.
        batch = kubernetes.client.BatchV1Api()
        job_complete = False
        job_failed = False
        job = None
        try:
            job = await asyncio.to_thread(batch.read_namespaced_job, job_name, namespace)
            conditions = getattr(getattr(job, "status", None), "conditions", None) or []
            job_complete = any(c.type == "Complete" and c.status == "True" for c in conditions)
            job_failed = any(c.type == "Failed" and c.status == "True" for c in conditions)
        except kubernetes.client.exceptions.ApiException as exc:
            if exc.status != 404:
                logger.warning(
                    "run_reconcile_timer: could not read job %s/%s: %s", namespace, job_name, exc
                )
        except Exception as exc:
            logger.warning(
                "run_reconcile_timer: could not read job %s/%s: %s", namespace, job_name, exc
            )

        if job_complete:
            logger.info(
                "K8siBackupRun %s/%s: job completed, run still Running — reconciling to Succeeded",
                namespace,
                name,
            )
            completion = now.isoformat()
            await asyncio.to_thread(
                _patch_run_status,
                namespace,
                name,
                {
                    "phase": "Succeeded",
                    "completionTime": completion,
                    "message": "reconciled from completed job",
                },
            )
            backup_name = meta.get("labels", {}).get("k8si.io/backup", "")
            if backup_name:
                custom = kubernetes.client.CustomObjectsApi()
                try:
                    backup_obj = await asyncio.to_thread(
                        custom.get_namespaced_custom_object,
                        "k8si.io",
                        "v1",
                        namespace,
                        "k8sibackups",
                        backup_name,
                    )
                    backup_spec = backup_obj.get("spec", {})
                    duration = int(
                        (now - datetime.fromisoformat(start_str or now.isoformat())).total_seconds()
                    )
                    await _update_parent_backup(
                        custom,
                        backup_name,
                        namespace,
                        name,
                        "success",
                        {},
                        backup_obj,
                        backup_spec,
                        duration,
                    )
                except Exception as e:
                    logger.warning(
                        "run_reconcile_timer: could not update parent %s/%s: %s",
                        namespace,
                        backup_name,
                        e,
                    )
            return

        if job_failed:
            message = "job failed — reconciled by timer"
        elif start_str and age_min >= _stuck_run_threshold_min(job):
            logger.warning(
                "K8siBackupRun %s/%s stuck Running for %.0f min, marking Failed",
                namespace,
                name,
                age_min,
            )
            message = f"stuck in Running for {age_min:.0f}m — job may have crashed"

    if message is None:
        return

    completion = now.isoformat()
    await asyncio.to_thread(
        _patch_run_status,
        namespace,
        name,
        {"phase": "Failed", "completionTime": completion, "message": message},
    )

    # Delete any associated K8s Job so it doesn't continue running orphaned.
    batch = kubernetes.client.BatchV1Api()
    try:
        await asyncio.to_thread(
            batch.delete_namespaced_job,
            status.get("jobName") or name,
            namespace,
            propagation_policy="Background",
        )
        logger.info("Deleted orphaned Job %s/%s", namespace, status.get("jobName") or name)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status != 404:
            logger.warning("Could not delete orphaned Job %s/%s: %s", namespace, name, exc)

    backup_name = meta.get("labels", {}).get("k8si.io/backup", "")
    if not backup_name:
        return

    custom = kubernetes.client.CustomObjectsApi()
    try:
        backup_obj = await asyncio.to_thread(
            custom.get_namespaced_custom_object,
            "k8si.io",
            "v1",
            namespace,
            "k8sibackups",
            backup_name,
        )
    except Exception as e:
        logger.warning(
            "run_reconcile_timer: could not fetch parent %s/%s: %s", namespace, backup_name, e
        )
        return

    backup_spec = backup_obj.get("spec", {})
    duration = int(
        (
            now
            - datetime.fromisoformat(
                status.get("startTime") or meta.get("creationTimestamp", now.isoformat())
            )
        ).total_seconds()
    )

    await _update_parent_backup(
        custom,
        backup_name,
        namespace,
        name,
        "failed",
        {},
        backup_obj,
        backup_spec,
        duration,
        error=message,
    )


async def _run_has_live_job(namespace: str, run_name: str) -> bool:
    """True if this run is already Running with a live K8s Job.

    After an operator restart, kopf re-invokes on_run_create for unfinished
    runs while the in-memory _running set is empty — re-executing would run a
    second Job against the same PVC/repo (#8)."""
    try:
        custom = kubernetes.client.CustomObjectsApi()
        run = await asyncio.to_thread(
            custom.get_namespaced_custom_object,
            "k8si.io",
            "v1",
            namespace,
            "k8sibackupruns",
            run_name,
        )
    except Exception:
        return False
    status = run.get("status", {})
    if status.get("phase") != "Running":
        return False
    job_name = status.get("jobName")
    if not job_name:
        return False
    try:
        batch = kubernetes.client.BatchV1Api()
        await asyncio.to_thread(batch.read_namespaced_job, str(job_name), namespace)
        return True
    except Exception:
        return False


@kopf.on.create("k8si.io", "v1", "k8sibackupruns")  # type: ignore[arg-type]
async def on_run_create(
    body: dict,
    spec: dict,
    name: str,
    namespace: str,
    logger: logging.Logger,
    **_: object,
) -> None:
    triggered_by = spec.get("triggeredBy", "schedule")
    backup_name = spec["backupRef"]
    key = (namespace, backup_name)

    if triggered_by == "backfill":
        logger.info("K8siBackupRun %s/%s is backfilled, skipping execution", namespace, name)
        return

    if key in _running:
        logger.warning("Concurrent on_run_create for %s/%s, marking Failed", namespace, backup_name)
        await asyncio.to_thread(
            _patch_run_status,
            namespace,
            name,
            {
                "phase": "Failed",
                "completionTime": datetime.now(tz=UTC).isoformat(),
                "message": "concurrent run rejected",
            },
        )
        return

    # Operator restart: kopf re-invokes this handler for unfinished runs while
    # _running is empty. If the original attempt's Job is still alive, refuse —
    # the reconciler finishes the run from the Job state instead (#8).
    if await _run_has_live_job(namespace, name):
        logger.warning(
            "K8siBackupRun %s/%s already Running with a live Job (operator restart?) —"
            " leaving it to the reconciler instead of duplicating the backup",
            namespace,
            name,
        )
        return

    _running.add(key)

    custom = kubernetes.client.CustomObjectsApi()
    try:
        backup_obj = await asyncio.to_thread(
            custom.get_namespaced_custom_object,
            "k8si.io",
            "v1",
            namespace,
            "k8sibackups",
            backup_name,
        )
    except Exception as e:
        logger.error("K8siBackupRun %s: could not get parent backup %s: %s", name, backup_name, e)
        _running.discard(key)
        try:
            await asyncio.to_thread(
                _patch_run_status,
                namespace,
                name,
                {
                    "phase": "Failed",
                    "completionTime": datetime.now(tz=UTC).isoformat(),
                    "message": str(e),
                },
            )
        except Exception:
            pass
        return

    backup_spec = backup_obj.get("spec", {})
    run_mode = spec.get("mode")
    if run_mode:
        backup_spec = {**backup_spec, "backupMode": run_mode}
    start_time = datetime.now(tz=UTC).isoformat()

    backup_start_dt = datetime.now(tz=UTC)
    try:
        await asyncio.to_thread(
            _patch_run_status, namespace, name, {"phase": "Running", "startTime": start_time}
        )
        result = await workflow.run_backup(
            backup_name,
            namespace,
            backup_spec,
            logger,
            body=backup_obj,
            run_name=name,
            run_ns=namespace,
        )
        duration = int((datetime.now(tz=UTC) - backup_start_dt).total_seconds())
        completion = datetime.now(tz=UTC).isoformat()

        # Guard: timer may have killed the run while backup was executing.
        # Re-read current phase; if already Failed, don't overwrite.
        try:
            current = await asyncio.to_thread(
                custom.get_namespaced_custom_object,
                "k8si.io",
                "v1",
                namespace,
                "k8sibackupruns",
                name,
            )
            if current.get("status", {}).get("phase") == "Failed":
                logger.warning(
                    "K8siBackupRun %s/%s was killed by timer during execution,"
                    " skipping Succeeded patch",
                    namespace,
                    name,
                )
                return
        except Exception as e:
            logger.warning(
                "Could not re-read run %s/%s: %s — proceeding with Succeeded",
                namespace,
                name,
                e,
            )

        artifact_patch: dict[str, Any] = {
            "phase": "Succeeded",
            "completionTime": completion,
            "message": "",
        }
        if result.get("jobName"):
            # The reconciler reads/deletes the Job by this name (#5).
            artifact_patch["jobName"] = result["jobName"]
        if result.get("snapshotId"):
            artifact_patch["snapshotId"] = result["snapshotId"]
        if result.get("sizeBytes") is not None:
            artifact_patch["sizeBytes"] = result["sizeBytes"]
        if result.get("backendType"):
            artifact_patch["backendType"] = result["backendType"]
        await asyncio.to_thread(_patch_run_status, namespace, name, artifact_patch)
        await _update_parent_backup(
            custom,
            backup_name,
            namespace,
            name,
            "success",
            result,
            backup_obj,
            backup_spec,
            duration,
        )
    except Exception as e:
        duration = int((datetime.now(tz=UTC) - backup_start_dt).total_seconds())
        logger.error("K8siBackupRun %s/%s failed: %s", namespace, name, e)
        completion = datetime.now(tz=UTC).isoformat()
        # Update parent BEFORE marking run Failed — SSE detects phase=Failed and
        # immediately calls loadBackups(); parent must already reflect the failure.
        await _update_parent_backup(
            custom,
            backup_name,
            namespace,
            name,
            "failed",
            {},
            backup_obj,
            backup_spec,
            duration,
            error=str(e),
        )
        await asyncio.to_thread(
            _patch_run_status,
            namespace,
            name,
            {"phase": "Failed", "completionTime": completion, "message": str(e)},
        )
    finally:
        _running.discard(key)


async def _update_parent_backup(
    custom: Any,
    backup_name: str,
    namespace: str,
    run_name: str,
    result: str,
    run_result: dict,
    backup_obj: dict,
    backup_spec: dict,
    duration: int,
    error: str = "",
) -> None:
    """Update parent K8siBackup status after a run completes."""
    status = backup_obj.get("status", {})
    schedule = backup_spec.get("schedule", "0 2 * * *")
    now_iso = run_result.get("lastBackupTime") or datetime.now(tz=UTC).isoformat()

    recent_backups = list(status.get("recentBackups", []))
    recent_backups.insert(0, {"time": now_iso, "result": result})

    recent_runs = list(status.get("recentRuns", []))
    run_entry: dict[str, Any] = {"name": run_name, "time": now_iso, "result": result}
    if run_result.get("snapshotId"):
        run_entry["snapshotId"] = run_result["snapshotId"]
    if run_result.get("sizeBytes") is not None:
        run_entry["sizeBytes"] = run_result["sizeBytes"]
    if run_result.get("backendType"):
        run_entry["backendType"] = run_result["backendType"]
    recent_runs.insert(0, run_entry)

    fields: dict[str, Any] = {
        "lastRunRef": run_name,
        "recentRuns": recent_runs[:30],
        # Legacy fields — kept for v0.8.0 backward compat
        "lastBackupResult": result,
        "lastBackupDuration": duration,
        "nextBackupTime": compute_next_backup(schedule),
        "recentBackups": recent_backups[:30],
        "message": error or run_result.get("message", ""),
    }
    if result == "success":
        fields["lastSuccessfulRunRef"] = run_name
        fields["lastBackupTime"] = now_iso

    try:
        await asyncio.to_thread(
            custom.patch_namespaced_custom_object_status,
            "k8si.io",
            "v1",
            namespace,
            "k8sibackups",
            backup_name,
            {"status": fields},
        )
    except Exception as e:
        log.warning("Failed to update parent K8siBackup %s/%s: %s", namespace, backup_name, e)

    metrics.record(
        backup_name, namespace, result, now_iso if result == "success" else None, duration=duration
    )

    if result == "success":
        webhook_url = backup_spec.get("notifyOnSuccess")
    else:
        webhook_url = backup_spec.get("notifyOnFailure")
    if webhook_url:
        await _notify_webhook(
            webhook_url,
            {
                "name": backup_name,
                "namespace": namespace,
                "result": result,
                "message": error or run_result.get("message", ""),
                "time": now_iso,
                "duration": duration,
            },
        )
