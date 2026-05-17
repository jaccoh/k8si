"""Full snapshot-first backup pipeline: quiesce → snapshot → backup job → cleanup."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import kubernetes
import kubernetes.client
import kubernetes.client.exceptions

from . import quiesce, snapshot
from .cronjob import K8SI_IMAGE

log = logging.getLogger(__name__)

_BACKUP_JOB_TIMEOUT = 3600
_HOOK_JOB_TIMEOUT = 300
_JOB_GONE_TIMEOUT = 120


async def run_backup(
    name: str, namespace: str, spec: dict[str, Any], logger: logging.Logger
) -> dict[str, str]:
    """Run the full snapshot-first backup. Returns status fields on success."""
    pvc_name = spec["pvc"]
    restic_secret = spec["resticSecret"]
    snapshot_class = spec.get("volumeSnapshotClass") or None
    db_spec = spec.get("database")
    hook = spec.get("preSnapshotHook")
    hook_required = spec.get("preSnapshotHookRequired", False)
    tags = spec.get("tags", [])
    retention = spec.get("retention", {})

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")
    snap_name = f"k8si-{name}-{ts}"
    snap_pvc = f"k8si-snap-{name}-{ts}"
    job_name = f"k8si-{name}-{ts}"

    # Phase 1: quiesce DB, run optional pre-snapshot hook, take snapshot, unquiesce
    async with quiesce.quiesce_context(db_spec, namespace, logger):
        if hook:
            await _run_hook_job(hook, hook_required, namespace, pvc_name, logger)
        await snapshot.create_snapshot(snap_name, namespace, pvc_name, snapshot_class)

    # Phase 2: create ephemeral PVC from snapshot, run restic backup, clean up
    snap_pvc_created = False
    node = await asyncio.to_thread(_find_pvc_node_sync, pvc_name, namespace)
    if node:
        logger.info("Pinning backup job to node %s (PVC %s)", node, pvc_name)
    try:
        await snapshot.create_pvc_from_snapshot(snap_pvc, namespace, snap_name, pvc_name)
        snap_pvc_created = True
        job_body = _build_backup_job(job_name, namespace, snap_pvc, restic_secret, spec, tags, retention, node)
        await _run_job(job_body, namespace, timeout=_BACKUP_JOB_TIMEOUT, logger=logger)
    finally:
        await snapshot.delete_snapshot_and_pvc(
            namespace, snap_name, snap_pvc if snap_pvc_created else None
        )

    now = datetime.now(tz=timezone.utc).isoformat()
    return {"lastBackupResult": "success", "lastBackupTime": now, "message": ""}


def _find_pvc_node_sync(pvc_name: str, namespace: str) -> str | None:
    """Return the node name where pvc_name is currently mounted, or None."""
    v1 = kubernetes.client.CoreV1Api()
    for pod in v1.list_namespaced_pod(namespace).items:
        for vol in (pod.spec.volumes or []):
            if (
                vol.persistent_volume_claim
                and vol.persistent_volume_claim.claim_name == pvc_name
                and pod.spec.node_name
            ):
                return pod.spec.node_name
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
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S%f")[:17]
    job_name = f"k8si-hook-{ts}"
    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "volumes": [
            {"name": "data", "persistentVolumeClaim": {"claimName": pvc_name}},
        ],
        "containers": [{
            "name": "k8si-hook",
            "image": K8SI_IMAGE,
            "command": [hook],
            "env": [{"name": "DATA_PATH", "value": "/data"}],
            "volumeMounts": [{"name": "data", "mountPath": "/data"}],
            "resources": {
                "requests": {"cpu": "50m", "memory": "64Mi"},
                "limits": {"cpu": "200m", "memory": "256Mi"},
            },
        }],
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
        {"name": "RETENTION_DAILY", "value": str(retention.get("daily", 7))},
        {"name": "RETENTION_WEEKLY", "value": str(retention.get("weekly", 4))},
        {"name": "RETENTION_MONTHLY", "value": str(retention.get("monthly", 3))},
    ]
    if tags:
        env.append({"name": "BACKUP_TAGS", "value": ",".join(tags)})
    for var, key in [
        ("RESTIC_REPOSITORY", "RESTIC_REPOSITORY"),
        ("RESTIC_PASSWORD", "RESTIC_PASSWORD"),
        ("RESTIC_SFTP_COMMAND", "RESTIC_SFTP_COMMAND"),
    ]:
        env.append({
            "name": var,
            "valueFrom": {"secretKeyRef": {"name": restic_secret, "key": key}},
        })

    resources = spec.get("resources", {
        "requests": {"cpu": "50m", "memory": "64Mi"},
        "limits": {"cpu": "200m", "memory": "256Mi"},
    })

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
                    "containers": [{
                        "name": "k8si",
                        "image": K8SI_IMAGE,
                        "env": env,
                        "volumeMounts": [
                            {"name": "data", "mountPath": "/data"},
                            {"name": "restic-ssh", "mountPath": "/restic-ssh", "readOnly": True},
                        ],
                        "resources": resources,
                    }],
                }
            },
        },
    }


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
            logs = _collect_job_logs(v1, job_name, namespace)
            raise RuntimeError(f"Job {job_name} failed.\n{logs}")
        time.sleep(10)
    raise TimeoutError(f"Job {job_name} timed out after {timeout}s")


def _collect_job_logs(v1: Any, job_name: str, namespace: str) -> str:
    try:
        pods = v1.list_namespaced_pod(namespace, label_selector=f"job-name={job_name}")
        for pod in pods.items:
            return v1.read_namespaced_pod_log(pod.metadata.name, namespace, tail_lines=50)
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
