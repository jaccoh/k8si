"""Full snapshot-first backup pipeline: quiesce → snapshot → backup job → cleanup.

Orchestration only — Job body construction lives in job_builder.py, artifact
parsing in artifacts.py (both re-exported here for historical importers).
"""

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import kopf
import kubernetes
import kubernetes.client
import kubernetes.client.exceptions

from . import artifacts, pool, quiesce, snapshot
from .artifacts import _parse_artifact  # re-exported for historical importers
from .cronjob import K8SI_IMAGE
from .job_builder import (
    _BACKUP_JOB_TIMEOUT,
    _build_backup_job,  # re-exported for historical importers
    _resolve_backup_secret,  # re-exported for historical importers
)

# Cap re-exported for tests / observability.
_MAX_CONCURRENT_BACKUPS = pool.MAX_CONCURRENT_BACKUPS

log = logging.getLogger(__name__)

BACKEND_TYPE: str = os.environ.get("BACKEND_TYPE", "restic").lower().strip()


def _effective_backend_type(spec: dict[str, Any]) -> str:
    """Per-backup backend: spec.backendType overrides the operator-wide
    BACKEND_TYPE (CRD contract; mirrors build_restore_patch)."""
    override = str(spec.get("backendType") or "").strip().lower()
    return override or BACKEND_TYPE


_HOOK_JOB_TIMEOUT = 300
_JOB_GONE_TIMEOUT = 120


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
    """Delete any leftover k8si-snap-{name}-<timestamp> PVCs from previous crashed runs.

    Matches the exact naming scheme used when the PVC is created
    (f"k8si-snap-{name}-{ts}" where ts is a %Y%m%d%H%M%S timestamp), anchored
    so that e.g. backup "app" never matches PVCs belonging to backup "app-db"
    (a plain prefix check on "k8si-snap-app-" would incorrectly match
    "k8si-snap-app-db-<ts>" too).
    """
    pattern = re.compile(rf"^k8si-snap-{re.escape(name)}-\d+$")
    snap_pattern = re.compile(rf"^k8si-{re.escape(name)}-\d+$")
    v1 = kubernetes.client.CoreV1Api()

    def _delete_orphans() -> None:
        # PVCs still mounted by any pod in the namespace are in active use —
        # e.g. the Job of an original run whose operator restarted and
        # re-invoked us (#8). Never sweep those.
        mounted: set[str] = set()
        for pod in v1.list_namespaced_pod(namespace).items:
            for vol in pod.spec.volumes or []:
                if vol.persistent_volume_claim and vol.persistent_volume_claim.claim_name:
                    mounted.add(vol.persistent_volume_claim.claim_name)

        pvcs = v1.list_namespaced_persistent_volume_claim(namespace)
        for pvc in pvcs.items:
            if pattern.match(pvc.metadata.name) and pvc.metadata.name not in mounted:
                log.warning("Deleting orphaned snapshot PVC %s/%s", namespace, pvc.metadata.name)
                try:
                    v1.delete_namespaced_persistent_volume_claim(pvc.metadata.name, namespace)
                except Exception as exc:
                    log.error("Failed to delete orphan PVC %s: %s", pvc.metadata.name, exc)

        # Leftover VolumeSnapshots wedge every later run in the 30-minute
        # conflict wait just like the PVCs do (#4). Best-effort: a cluster
        # without the snapshot CRDs (or a failing list) must not break the run.
        try:
            custom = kubernetes.client.CustomObjectsApi()
            snaps = custom.list_namespaced_custom_object(
                "snapshot.storage.k8s.io", "v1", namespace, "volumesnapshots"
            )
        except Exception as exc:
            log.debug("Could not list VolumeSnapshots in %s: %s", namespace, exc)
            snaps = {"items": []}
        for snap in snaps.get("items", []):
            snap_name = snap.get("metadata", {}).get("name", "")
            if snap_pattern.match(snap_name):
                log.warning("Deleting orphaned VolumeSnapshot %s/%s", namespace, snap_name)
                try:
                    custom.delete_namespaced_custom_object(
                        "snapshot.storage.k8s.io", "v1", namespace, "volumesnapshots", snap_name
                    )
                except Exception as exc:
                    log.error("Failed to delete orphan snapshot %s: %s", snap_name, exc)

    await asyncio.to_thread(_delete_orphans)


async def run_backup(
    name: str,
    namespace: str,
    spec: dict[str, Any],
    logger: logging.Logger,
    body: dict[str, Any] | None = None,
    run_name: str = "",
    run_ns: str | None = None,
    on_job_created: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Run the full snapshot-first backup. Returns status fields on success.

    Concurrency-capped: each execution parks an executor worker for the whole
    job duration — unbounded parallel backups froze the operator (#6).
    """
    async with pool.SEMAPHORE:
        return await _run_backup(
            name, namespace, spec, logger, body, run_name, run_ns, on_job_created
        )


async def _run_backup(
    name: str,
    namespace: str,
    spec: dict[str, Any],
    logger: logging.Logger,
    body: dict[str, Any] | None = None,
    run_name: str = "",
    run_ns: str | None = None,
    on_job_created: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Run the full snapshot-first backup. Returns status fields on success.

    Live log entries are patched to the K8siBackupRun named by *run_name*.
    """
    if not run_name:
        raise ValueError("run_name is required: log entries are patched to the K8siBackupRun")
    run_log: list[dict] = []
    _effective_run_ns = run_ns or namespace

    async def _log_phase(phase: str, message: str) -> None:
        run_log.append(
            {"time": datetime.now(tz=UTC).isoformat(), "phase": phase, "message": message}
        )
        await asyncio.to_thread(_patch_run_status, _effective_run_ns, run_name, {"log": run_log})

    await asyncio.to_thread(_patch_run_status, _effective_run_ns, run_name, {"log": []})

    await _cleanup_orphan_snap_pvcs(name, namespace)
    pvc_name = spec["pvc"]
    backend_type = _effective_backend_type(spec)
    restic_secret = _resolve_backup_secret(spec, backend_type)
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
    repo_pvc = spec.get("repositoryPVC") or None
    job_timeout = int(spec.get("jobTimeout", _BACKUP_JOB_TIMEOUT))

    if backup_mode == "direct":
        if db_spec:
            _emit_event(body, "Normal", "QuiesceStarted", f"Quiescing DB type: {db_spec['type']}")
            await _log_phase("QuiesceStarted", f"Quiescing DB type: {db_spec['type']}")
        try:
            async with quiesce.quiesce_context(db_spec, namespace, logger):
                if hook:
                    _emit_event(body, "Normal", "HookStarted", f"Running pre-snapshot hook: {hook}")
                    await _log_phase("HookStarted", f"Running pre-snapshot hook: {hook}")
                    await _run_hook_job(hook, hook_required, namespace, pvc_name, logger)

                node = await asyncio.to_thread(_find_pvc_node_sync, pvc_name, namespace)
                if node:
                    logger.info("Pinning backup job to node %s (PVC %s)", node, pvc_name)

                job_body = _build_backup_job(
                    job_name,
                    namespace,
                    pvc_name,
                    restic_secret,
                    spec,
                    tags,
                    retention,
                    node,
                    repo_pvc,
                    job_timeout,
                    backend_type=backend_type,
                )
                _emit_event(body, "Normal", "BackupJobStarted", f"Starting Job {job_name}")
                await _log_phase("BackupJobStarted", f"Starting Job {job_name}")
                if on_job_created:
                    await on_job_created(job_name)
                raw_logs = await _run_job(job_body, namespace, timeout=job_timeout, logger=logger)
                _emit_event(body, "Normal", "BackupJobCompleted", f"Job {job_name} completed")
                await _log_phase("BackupJobCompleted", f"Job {job_name} completed")
        except Exception as e:
            _emit_event(body, "Warning", "BackupFailed", f"Direct backup failed: {e}")
            await _log_phase("BackupFailed", f"Direct backup failed: {e}")
            raise
    else:
        if db_spec:
            _emit_event(body, "Normal", "QuiesceStarted", f"Quiescing DB type: {db_spec['type']}")
            await _log_phase("QuiesceStarted", f"Quiescing DB type: {db_spec['type']}")
        # Phase 1: quiesce DB, run optional pre-snapshot hook, take snapshot, unquiesce
        try:
            async with quiesce.quiesce_context(db_spec, namespace, logger):
                if hook:
                    _emit_event(body, "Normal", "HookStarted", f"Running pre-snapshot hook: {hook}")
                    await _log_phase("HookStarted", f"Running pre-snapshot hook: {hook}")
                    await _run_hook_job(hook, hook_required, namespace, pvc_name, logger)
                _emit_event(body, "Normal", "SnapshotStarted", f"Creating snapshot {snap_name}")
                await _log_phase("SnapshotStarted", f"Creating snapshot {snap_name}")
                await snapshot.create_snapshot(snap_name, namespace, pvc_name, snapshot_class)
                _emit_event(body, "Normal", "SnapshotCreated", f"Snapshot {snap_name} ready")
                await _log_phase("SnapshotCreated", f"Snapshot {snap_name} ready")
        except Exception as e:
            _emit_event(body, "Warning", "SnapshotFailed", f"Snapshot phase failed: {e}")
            await _log_phase("SnapshotFailed", f"Snapshot phase failed: {e}")
            # A VolumeSnapshot stuck not-Ready wedges every later run for 30
            # minutes inside the snapshot-conflict wait — delete it (#4).
            try:
                await snapshot.delete_snapshot_and_pvc(namespace, snap_name, None)
            except Exception as cleanup_exc:
                logger.warning("Failed to clean up snapshot %s: %s", snap_name, cleanup_exc)
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
                job_name,
                namespace,
                snap_pvc,
                restic_secret,
                spec,
                tags,
                retention,
                node,
                repo_pvc,
                job_timeout,
                backend_type=backend_type,
            )
            _emit_event(body, "Normal", "BackupJobStarted", f"Starting backup Job {job_name}")
            await _log_phase("BackupJobStarted", f"Starting backup Job {job_name}")
            if on_job_created:
                await on_job_created(job_name)
            raw_logs = await _run_job(job_body, namespace, timeout=job_timeout, logger=logger)
            _emit_event(body, "Normal", "BackupJobCompleted", f"Backup Job {job_name} completed")
            await _log_phase("BackupJobCompleted", f"Backup Job {job_name} completed")
        except Exception as e:
            _emit_event(body, "Warning", "BackupFailed", f"Backup phase failed: {e}")
            await _log_phase("BackupFailed", f"Backup phase failed: {e}")
            raise
        finally:
            await snapshot.delete_snapshot_and_pvc(
                namespace, snap_name, snap_pvc if snap_pvc_created else None
            )

    snapshot_id, size_bytes = _parse_artifact(raw_logs, backend_type)
    if snapshot_id:
        logger.info("Artifact: snapshot %s, size %s B", snapshot_id, size_bytes)
    else:
        raw = raw_logs or ""
        marker = artifacts.ARTIFACT_MARKER.strip()
        idx = raw.find(marker)
        context = raw[max(0, idx - 150) : idx + 250] if idx != -1 else raw[-200:]
        logger.warning(
            "Could not parse snapshot ID from job %s logs (marker present: %s, marker context: %r)",
            job_name,
            idx != -1,
            context,
        )

    now = datetime.now(tz=UTC).isoformat()
    return {
        "lastBackupResult": "success",
        "lastBackupTime": now,
        "message": "",
        "snapshotId": snapshot_id,
        "sizeBytes": size_bytes,
        "backendType": backend_type,
        # The actual Job name (k8si-{backup}-{ts}) — recorded on the run so
        # the reconciler can find/delete the Job (#5); it never equals the
        # run name, which is what the reconciler used to look up.
        "jobName": job_name,
    }


_TERMINAL_POD_PHASES = frozenset({"Succeeded", "Failed"})


def _find_pvc_node_sync(pvc_name: str, namespace: str) -> str | None:
    """Return the node name where pvc_name is currently mounted, or None.

    Terminal pods (Succeeded/Failed) are skipped: their spec still lists the
    PVC and their node assignment lingers after death, so a dead pod would pin
    the backup Job to a node the volume is no longer attached to.
    """
    v1 = kubernetes.client.CoreV1Api()
    for pod in v1.list_namespaced_pod(namespace).items:
        if getattr(pod.status, "phase", None) in _TERMINAL_POD_PHASES:
            continue
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
    v1 = kubernetes.client.CoreV1Api()
    deadline = time.monotonic() + _JOB_GONE_TIMEOUT
    while time.monotonic() < deadline:
        try:
            batch.read_namespaced_job(job_name, namespace)
            time.sleep(3)
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                break
            raise

    pod_deadline = time.monotonic() + 30
    while time.monotonic() < pod_deadline:
        try:
            pods = v1.list_namespaced_pod(namespace, label_selector=f"job-name={job_name}").items
            if not pods:
                break
        except Exception:
            break
        time.sleep(2)
    time.sleep(5)


async def _run_job(
    job_body: dict[str, Any], namespace: str, timeout: int, logger: logging.Logger
) -> str:
    """Run a K8s Job, collect its pod logs on success, then delete it. Returns logs string."""
    job_name = job_body["metadata"]["name"]
    batch = kubernetes.client.BatchV1Api()
    v1 = kubernetes.client.CoreV1Api()
    await asyncio.to_thread(batch.create_namespaced_job, namespace, job_body)
    logger.info("Created Job %s/%s", namespace, job_name)
    raw_logs = ""
    try:
        await pool.to_pool(_wait_job_complete_sync, job_name, namespace, timeout)
        logger.info("Job %s/%s completed", namespace, job_name)
        # Collect logs before pod deletion — pods are gone after Foreground delete.
        raw_logs = await asyncio.to_thread(_collect_job_logs, v1, job_name, namespace)
    finally:
        try:
            await asyncio.to_thread(
                batch.delete_namespaced_job, job_name, namespace, propagation_policy="Foreground"
            )
            await pool.to_pool(_wait_job_gone_sync, job_name, namespace)
        except Exception:
            pass
    return raw_logs
