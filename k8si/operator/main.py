"""Kopf operator: reconciles K8siBackup CRDs into CronJobs."""

import logging

import kopf
import kubernetes
import kubernetes.client
import kubernetes.client.exceptions

from .cronjob import K8SI_IMAGE, build_cronjob, build_restore_patch
from .status import compute_next_backup, infer_result

log = logging.getLogger(__name__)


def _pvc_node(namespace: str, pvc_name: str) -> str | None:
    """Return the node currently running a pod that mounts the given PVC, or None."""
    v1 = kubernetes.client.CoreV1Api()
    pods = v1.list_namespaced_pod(namespace)
    for pod in pods.items:
        for vol in pod.spec.volumes or []:
            if vol.persistent_volume_claim and vol.persistent_volume_claim.claim_name == pvc_name:
                return pod.spec.node_name
    return None


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
    uid: str,
    patch: kopf.Patch,
    logger: logging.Logger,
    **_: object,
) -> None:
    api = kubernetes.client.BatchV1Api()
    node = _pvc_node(namespace, spec["pvc"])
    if node:
        logger.info("PVC %s is on node %s, pinning backup job there", spec["pvc"], node)
    body = build_cronjob(name, namespace, uid, spec, node_name=node)
    api.create_namespaced_cron_job(namespace, body)
    logger.info("Created CronJob k8si-%s in %s", name, namespace)
    patch.status["nextBackupTime"] = compute_next_backup(spec["schedule"])
    patch.status["lastBackupResult"] = "pending"
    patch.status["message"] = f"CronJob k8si-{name} created"
    patch.status["restorePatch"] = build_restore_patch(spec)


@kopf.on.update("k8si.io", "v1", "k8sibackups")
def on_update(
    spec: dict,
    name: str,
    namespace: str,
    uid: str,
    logger: logging.Logger,
    **_: object,
) -> None:
    api = kubernetes.client.BatchV1Api()
    node = _pvc_node(namespace, spec["pvc"])
    body = build_cronjob(name, namespace, uid, spec, node_name=node)
    try:
        api.patch_namespaced_cron_job(f"k8si-{name}", namespace, body)
        logger.info("Updated CronJob k8si-%s", name)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            api.create_namespaced_cron_job(namespace, body)
            logger.info("CronJob k8si-%s was missing, recreated", name)
        else:
            raise


@kopf.on.delete("k8si.io", "v1", "k8sibackups")
def on_delete(name: str, logger: logging.Logger, **_: object) -> None:
    # Owner reference on the CronJob means Kubernetes GC handles deletion.
    logger.info("K8siBackup %s deleted; CronJob removed via owner reference", name)


# ── Status sync ────────────────────────────────────────────────────────────────

@kopf.timer("k8si.io", "v1", "k8sibackups", interval=300, idle=60)
def sync_status(
    spec: dict,
    name: str,
    namespace: str,
    patch: kopf.Patch,
    logger: logging.Logger,
    **_: object,
) -> None:
    api = kubernetes.client.BatchV1Api()
    try:
        cj = api.read_namespaced_cron_job(f"k8si-{name}", namespace)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            return
        raise

    cj_status = cj.status
    last_schedule = cj_status.last_schedule_time if cj_status else None
    last_success = cj_status.last_successful_time if cj_status else None

    result = infer_result(last_schedule, last_success)
    patch.status["lastBackupResult"] = result
    patch.status["nextBackupTime"] = compute_next_backup(spec["schedule"])

    if last_success:
        patch.status["lastBackupTime"] = last_success.isoformat()

    if result == "failed":
        logger.warning("Last backup for %s appears to have failed", name)
        patch.status["message"] = "Last scheduled run did not complete successfully"
    else:
        patch.status["message"] = ""
