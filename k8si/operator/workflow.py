"""Full snapshot-first backup pipeline: quiesce → snapshot → backup job → cleanup."""

import asyncio
import logging
import os
import re
import time
from datetime import UTC, datetime
from typing import Any

import kopf
import kubernetes
import kubernetes.client
import kubernetes.client.exceptions

from . import quiesce, snapshot
from .cronjob import K8SI_IMAGE, _restic_env_vars

log = logging.getLogger(__name__)

BACKEND_TYPE: str = os.environ.get("BACKEND_TYPE", "restic").lower().strip()

_SIZE_UNITS: dict[str, int] = {
    "b": 1,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
}


def _resolve_backup_secret(spec: dict[str, Any], backend_type: str) -> str:
    """Return the secret name to use for the backup job.

    kopia uses spec.kopiaSecret (falls back to resticSecret for shared SFTP secrets).
    restic always uses spec.resticSecret.
    """
    if backend_type == "kopia":
        return spec.get("kopiaSecret") or spec.get("resticSecret", "")
    return spec.get("resticSecret", "")


def _parse_artifact(logs: str, backend_type: str) -> tuple[str | None, int | None]:
    """Parse snapshot ID and total size in bytes from backup job stdout.

    Returns (snapshot_id, size_bytes). Either field may be None if not found.
    """
    if backend_type == "restic":
        snap_id: str | None = None
        m = re.search(r"snapshot ([a-f0-9]+) saved", logs)
        if m:
            snap_id = m.group(1)

        size_bytes: int | None = None
        # "processed N files, 23.456 GiB in 0:00"
        m2 = re.search(
            r"processed \d+ files?,\s+([\d.]+)\s*([A-Za-z]+)\s+in\b",
            logs,
        )
        if m2:
            amount, unit = float(m2.group(1)), m2.group(2).lower()
            multiplier = _SIZE_UNITS.get(unit)
            if multiplier is not None:
                size_bytes = int(amount * multiplier)

        return snap_id, size_bytes

    if backend_type == "kopia":
        snap_id = None
        # Modern kopia: "Created snapshot with root <root> and ID <id> in Ns."
        m = re.search(r"Created snapshot with root \S+ and ID (\S+)", logs)
        if m:
            snap_id = m.group(1).rstrip(".")
        else:
            # Legacy kopia: "Snapshotted source and ID <id> in 0:05"
            m = re.search(r"Snapshotted source and ID (\S+)", logs)
            if m:
                snap_id = m.group(1)

        size_bytes = None
        # Progress lines: "* N hashing, N cached (SIZE), N uploading"
        # Take the last match (final summary line has the largest/complete total).
        for m2 in re.finditer(r"cached \(([\d.]+)\s*([A-Za-z]+)\)", logs):
            amount = float(m2.group(1))
            unit = m2.group(2).lower()
            multiplier = _SIZE_UNITS.get(unit)
            if multiplier is not None:
                size_bytes = int(amount * multiplier)

        return snap_id, size_bytes

    return None, None


_BACKUP_JOB_TIMEOUT = 3600
_HOOK_JOB_TIMEOUT = 300
_JOB_GONE_TIMEOUT = 120


def _write_run_log(name: str, namespace: str, entries: list[dict]) -> None:
    """Directly PATCH K8siBackup status.lastRunLog. Best-effort — swallows all errors."""
    try:
        api = kubernetes.client.CustomObjectsApi()
        api.patch_namespaced_custom_object_status(
            group="k8si.io",
            version="v1",
            namespace=namespace,
            plural="k8sibackups",
            name=name,
            body={"status": {"lastRunLog": entries}},
        )
    except Exception:
        pass


def _patch_run_status(run_ns: str, run_name: str, fields: dict) -> None:
    """Directly PATCH K8siBackupRun status fields. Best-effort — logs on failure."""
    try:
        api = kubernetes.client.CustomObjectsApi()
        api.patch_namespaced_custom_object_status(
            group="k8si.io",
            version="v1",
            namespace=run_ns,
            plural="k8sibackupruns",
            name=run_name,
            body={"status": fields},
        )
    except Exception as exc:
        log.warning(
            "_patch_run_status %s/%s fields=%s failed: %s",
            run_ns,
            run_name,
            list(fields),
            exc,
        )


def _emit_event(body: dict[str, Any] | None, type_str: str, reason: str, message: str) -> None:
    if body is None:
        return
    try:
        kopf.event(body, type=type_str, reason=reason, message=message)  # type: ignore[arg-type]
    except Exception:
        pass


async def _cleanup_orphan_snap_pvcs(name: str, namespace: str) -> None:
    """Delete any leftover k8si-snap-{name}-* PVCs from previous crashed runs."""
    prefix = f"k8si-snap-{name}-"
    v1 = kubernetes.client.CoreV1Api()

    def _delete_orphans() -> None:
        pvcs = v1.list_namespaced_persistent_volume_claim(namespace)
        for pvc in pvcs.items:
            if pvc.metadata.name.startswith(prefix):
                log.warning("Deleting orphaned snapshot PVC %s/%s", namespace, pvc.metadata.name)
                try:
                    v1.delete_namespaced_persistent_volume_claim(pvc.metadata.name, namespace)
                except Exception as exc:
                    log.error("Failed to delete orphan PVC %s: %s", pvc.metadata.name, exc)

    await asyncio.to_thread(_delete_orphans)


async def run_backup(
    name: str,
    namespace: str,
    spec: dict[str, Any],
    logger: logging.Logger,
    body: dict[str, Any] | None = None,
    run_name: str | None = None,
    run_ns: str | None = None,
) -> dict[str, str]:
    """Run the full snapshot-first backup. Returns status fields on success.

    When run_name is provided, live log entries are patched to K8siBackupRun status.
    Otherwise they are patched to K8siBackup status (legacy, for tests).
    """
    run_log: list[dict] = []
    _effective_run_ns = run_ns or namespace

    if run_name:
        _patch_run_status(_effective_run_ns, run_name, {"log": []})
    else:
        _write_run_log(name, namespace, run_log)

    def _log_phase(phase: str, message: str) -> None:
        run_log.append(
            {"time": datetime.now(tz=UTC).isoformat(), "phase": phase, "message": message}
        )
        if run_name:
            _patch_run_status(_effective_run_ns, run_name, {"log": run_log})
        else:
            _write_run_log(name, namespace, run_log)

    await _cleanup_orphan_snap_pvcs(name, namespace)
    pvc_name = spec["pvc"]
    restic_secret = _resolve_backup_secret(spec, BACKEND_TYPE)
    snapshot_class = spec.get("volumeSnapshotClass") or None
    db_spec = spec.get("database")
    hook = spec.get("preSnapshotHook")
    hook_required = spec.get("preSnapshotHookRequired", False)
    tags = spec.get("tags", [])
    retention = spec.get("retention", {})

    ts = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S")
    snap_name = f"k8si-{name}-{ts}"
    snap_pvc = f"k8si-snap-{name}-{ts}"
    job_name = f"k8si-{name}-{ts}"
    backup_mode = spec.get("backupMode", "snapshot")

    if backup_mode == "direct":
        if db_spec:
            _emit_event(body, "Normal", "QuiesceStarted", f"Quiescing DB type: {db_spec['type']}")
            _log_phase("QuiesceStarted", f"Quiescing DB type: {db_spec['type']}")
        try:
            async with quiesce.quiesce_context(db_spec, namespace, logger):
                if hook:
                    _emit_event(body, "Normal", "HookStarted", f"Running pre-snapshot hook: {hook}")
                    _log_phase("HookStarted", f"Running pre-snapshot hook: {hook}")
                    await _run_hook_job(hook, hook_required, namespace, pvc_name, logger)

                node = await asyncio.to_thread(_find_pvc_node_sync, pvc_name, namespace)
                if node:
                    logger.info("Pinning backup job to node %s (PVC %s)", node, pvc_name)

                job_body = _build_backup_job(
                    job_name, namespace, pvc_name, restic_secret, spec, tags, retention, node
                )
                _emit_event(body, "Normal", "BackupJobStarted", f"Starting Job {job_name}")
                _log_phase("BackupJobStarted", f"Starting Job {job_name}")
                await _run_job(job_body, namespace, timeout=_BACKUP_JOB_TIMEOUT, logger=logger)
                _emit_event(body, "Normal", "BackupJobCompleted", f"Job {job_name} completed")
                _log_phase("BackupJobCompleted", f"Job {job_name} completed")
        except Exception as e:
            _emit_event(body, "Warning", "BackupFailed", f"Direct backup failed: {e}")
            _log_phase("BackupFailed", f"Direct backup failed: {e}")
            raise
    else:
        if db_spec:
            _emit_event(body, "Normal", "QuiesceStarted", f"Quiescing DB type: {db_spec['type']}")
            _log_phase("QuiesceStarted", f"Quiescing DB type: {db_spec['type']}")
        # Phase 1: quiesce DB, run optional pre-snapshot hook, take snapshot, unquiesce
        try:
            async with quiesce.quiesce_context(db_spec, namespace, logger):
                if hook:
                    _emit_event(body, "Normal", "HookStarted", f"Running pre-snapshot hook: {hook}")
                    _log_phase("HookStarted", f"Running pre-snapshot hook: {hook}")
                    await _run_hook_job(hook, hook_required, namespace, pvc_name, logger)
                _emit_event(body, "Normal", "SnapshotStarted", f"Creating snapshot {snap_name}")
                _log_phase("SnapshotStarted", f"Creating snapshot {snap_name}")
                await snapshot.create_snapshot(snap_name, namespace, pvc_name, snapshot_class)
                _emit_event(body, "Normal", "SnapshotCreated", f"Snapshot {snap_name} ready")
                _log_phase("SnapshotCreated", f"Snapshot {snap_name} ready")
        except Exception as e:
            _emit_event(body, "Warning", "SnapshotFailed", f"Snapshot phase failed: {e}")
            _log_phase("SnapshotFailed", f"Snapshot phase failed: {e}")
            raise

        # Phase 2: create ephemeral PVC from snapshot, run backup job, clean up
        snap_pvc_created = False
        node = await asyncio.to_thread(_find_pvc_node_sync, pvc_name, namespace)
        if node:
            logger.info("Pinning backup job to node %s (PVC %s)", node, pvc_name)
        try:
            await snapshot.create_pvc_from_snapshot(snap_pvc, namespace, snap_name, pvc_name)
            snap_pvc_created = True
            job_body = _build_backup_job(
                job_name, namespace, snap_pvc, restic_secret, spec, tags, retention, node
            )
            _emit_event(body, "Normal", "BackupJobStarted", f"Starting backup Job {job_name}")
            _log_phase("BackupJobStarted", f"Starting backup Job {job_name}")
            await _run_job(job_body, namespace, timeout=_BACKUP_JOB_TIMEOUT, logger=logger)
            _emit_event(body, "Normal", "BackupJobCompleted", f"Backup Job {job_name} completed")
            _log_phase("BackupJobCompleted", f"Backup Job {job_name} completed")
        except Exception as e:
            _emit_event(body, "Warning", "BackupFailed", f"Backup phase failed: {e}")
            _log_phase("BackupFailed", f"Backup phase failed: {e}")
            raise
        finally:
            await snapshot.delete_snapshot_and_pvc(
                namespace, snap_name, snap_pvc if snap_pvc_created else None
            )

    # Collect job logs to extract snapshot ID and backup size (best-effort, non-fatal)
    v1 = kubernetes.client.CoreV1Api()
    raw_logs = await asyncio.to_thread(_collect_job_logs, v1, job_name, namespace)
    snapshot_id, size_bytes = _parse_artifact(raw_logs, BACKEND_TYPE)
    if snapshot_id:
        logger.info("Artifact: snapshot %s, size %s B", snapshot_id, size_bytes)
    else:
        logger.warning("Could not parse snapshot ID from job %s logs", job_name)

    now = datetime.now(tz=UTC).isoformat()
    return {
        "lastBackupResult": "success",
        "lastBackupTime": now,
        "message": "",
        "snapshotId": snapshot_id,
        "sizeBytes": size_bytes,
        "backendType": BACKEND_TYPE,
    }


def _find_pvc_node_sync(pvc_name: str, namespace: str) -> str | None:
    """Return the node name where pvc_name is currently mounted, or None."""
    v1 = kubernetes.client.CoreV1Api()
    for pod in v1.list_namespaced_pod(namespace).items:
        for vol in pod.spec.volumes or []:
            if (
                vol.persistent_volume_claim
                and vol.persistent_volume_claim.claim_name == pvc_name
                and pod.spec.node_name
            ):
                return pod.spec.node_name  # type: ignore[no-any-return]
    return None


async def _run_hook_job(
    hook: str,
    required: bool,
    namespace: str,
    pvc_name: str,
    logger: logging.Logger,
) -> None:
    node = await asyncio.to_thread(_find_pvc_node_sync, pvc_name, namespace)
    if node:
        logger.info("Pinning hook job to node %s (PVC %s)", node, pvc_name)
    ts = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S%f")[:17]
    job_name = f"k8si-hook-{ts}"
    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "volumes": [
            {"name": "data", "persistentVolumeClaim": {"claimName": pvc_name}},
        ],
        "containers": [
            {
                "name": "k8si-hook",
                "image": K8SI_IMAGE,
                "command": [hook],
                "env": [{"name": "DATA_PATH", "value": "/data"}],
                "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                "resources": {
                    "requests": {"cpu": "50m", "memory": "128Mi"},
                    "limits": {"cpu": "200m", "memory": "1Gi"},
                },
            }
        ],
    }
    if node:
        pod_spec["nodeSelector"] = {"kubernetes.io/hostname": node}
    job_body = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name, "namespace": namespace},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 300,
            "template": {"spec": pod_spec},
        },
    }
    try:
        await _run_job(job_body, namespace, timeout=_HOOK_JOB_TIMEOUT, logger=logger)
    except Exception as e:
        if required:
            raise RuntimeError(f"Pre-snapshot hook {hook!r} failed: {e}") from e
        logger.error("Pre-snapshot hook failed (non-required, continuing): %s", e)


def _build_backup_job(
    job_name: str,
    namespace: str,
    pvc_name: str,
    restic_secret: str,
    spec: dict[str, Any],
    tags: list[str],
    retention: dict[str, int],
    node: str | None = None,
) -> dict[str, Any]:
    env: list[dict[str, Any]] = [
        {"name": "MODE", "value": "job"},
        {"name": "DATA_PATH", "value": "/data"},
        {"name": "BACKEND_TYPE", "value": BACKEND_TYPE},
        {"name": "RETENTION_DAILY", "value": str(retention.get("daily", 7))},
        {"name": "RETENTION_WEEKLY", "value": str(retention.get("weekly", 4))},
        {"name": "RETENTION_MONTHLY", "value": str(retention.get("monthly", 3))},
    ]
    if tags:
        env.append({"name": "BACKUP_TAGS", "value": ",".join(tags)})
    if spec.get("checkAfterBackup"):
        env.append({"name": "RUN_CHECK", "value": "true"})
    env.extend(_restic_env_vars(restic_secret))

    resources = spec.get(
        "resources",
        {
            "requests": {"cpu": "50m", "memory": "128Mi"},
            "limits": {"cpu": "200m", "memory": "1Gi"},
        },
    )

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name, "namespace": namespace},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 600,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    **({"nodeSelector": {"kubernetes.io/hostname": node}} if node else {}),
                    "volumes": [
                        {"name": "data", "persistentVolumeClaim": {"claimName": pvc_name}},
                        {
                            "name": "restic-ssh",
                            "secret": {
                                "secretName": restic_secret,
                                "defaultMode": 0o400,
                                "items": [
                                    {"key": "id_ed25519", "path": "id_ed25519"},
                                    {"key": "known_hosts", "path": "known_hosts"},
                                ],
                            },
                        },
                    ],
                    "containers": [
                        {
                            "name": "k8si",
                            "image": K8SI_IMAGE,
                            "env": env,
                            "volumeMounts": [
                                {"name": "data", "mountPath": "/data"},
                                {
                                    "name": "restic-ssh",
                                    "mountPath": "/restic-ssh",
                                    "readOnly": True,
                                },
                            ],
                            "resources": resources,
                        }
                    ],
                }
            },
        },
    }


def _get_pod_failure_reason(v1: Any, job_name: str, namespace: str) -> str:
    """Return a human-readable failure reason; detects OOMKill from exit code 137."""
    try:
        pods = v1.list_namespaced_pod(namespace, label_selector=f"job-name={job_name}")
        for pod in pods.items:
            for cs in pod.status.container_statuses or []:
                term = None
                if cs.state and cs.state.terminated:
                    term = cs.state.terminated
                elif cs.last_state and cs.last_state.terminated:
                    term = cs.last_state.terminated
                if term is None:
                    continue
                if term.reason == "OOMKilled" or term.exit_code == 137:
                    return (
                        "OOMKill: container killed by kernel (exit 137) — "
                        "increase spec.resources.limits.memory"
                    )
                if term.exit_code is not None:
                    return f"exit {term.exit_code}"
    except Exception:
        pass
    return "non-zero exit code"


def _wait_job_complete_sync(job_name: str, namespace: str, timeout: int) -> None:
    batch = kubernetes.client.BatchV1Api()
    v1 = kubernetes.client.CoreV1Api()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = batch.read_namespaced_job(job_name, namespace)
        status = job.status
        if status.succeeded and status.succeeded > 0:
            return
        if status.failed and status.failed > 0:
            reason = _get_pod_failure_reason(v1, job_name, namespace)
            logs = _collect_job_logs(v1, job_name, namespace)
            raise RuntimeError(f"Job {job_name} failed: {reason}.\n{logs}")
        time.sleep(10)
    raise TimeoutError(f"Job {job_name} timed out after {timeout}s")


def _collect_job_logs(v1: Any, job_name: str, namespace: str) -> str:
    try:
        pods = v1.list_namespaced_pod(namespace, label_selector=f"job-name={job_name}")
        for pod in pods.items:
            return v1.read_namespaced_pod_log(  # type: ignore[no-any-return]
                pod.metadata.name, namespace
            )
    except Exception:
        pass
    return ""


def _wait_job_gone_sync(job_name: str, namespace: str) -> None:
    batch = kubernetes.client.BatchV1Api()
    deadline = time.monotonic() + _JOB_GONE_TIMEOUT
    while time.monotonic() < deadline:
        try:
            batch.read_namespaced_job(job_name, namespace)
            time.sleep(3)
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                return
            raise
    log.warning("Job %s still present after %ss; proceeding", job_name, _JOB_GONE_TIMEOUT)


async def _run_job(
    job_body: dict[str, Any], namespace: str, timeout: int, logger: logging.Logger
) -> None:
    job_name = job_body["metadata"]["name"]
    batch = kubernetes.client.BatchV1Api()
    await asyncio.to_thread(batch.create_namespaced_job, namespace, job_body)
    logger.info("Created Job %s/%s", namespace, job_name)
    try:
        await asyncio.to_thread(_wait_job_complete_sync, job_name, namespace, timeout)
        logger.info("Job %s/%s completed", namespace, job_name)
    finally:
        try:
            await asyncio.to_thread(
                batch.delete_namespaced_job, job_name, namespace, propagation_policy="Foreground"
            )
            await asyncio.to_thread(_wait_job_gone_sync, job_name, namespace)
        except Exception:
            pass
