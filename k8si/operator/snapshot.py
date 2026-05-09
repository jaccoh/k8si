"""VolumeSnapshot lifecycle via the Kubernetes Python client."""

import asyncio
import logging
import time

import kubernetes
import kubernetes.client
import kubernetes.client.exceptions

log = logging.getLogger(__name__)

_SNAPSHOT_GROUP = "snapshot.storage.k8s.io"
_SNAPSHOT_VERSION = "v1"
_SNAPSHOT_PLURAL = "volumesnapshots"

_SNAPSHOT_READY_TIMEOUT = 300
_PVC_BOUND_TIMEOUT = 120
_JOB_GONE_TIMEOUT = 120

# 1-replica, Delete-policy StorageClass for ephemeral backup PVCs.
# Avoids the degraded-state issue that Longhorn multi-replica clones hit
# while building their second replica before the backup job can attach.
_EPHEMERAL_STORAGE_CLASS = "longhorn-k8si-ephemeral"


def _get_pvc_info_sync(pvc_name: str, namespace: str) -> tuple[str, str]:
    """Returns (accessMode, storage) from source PVC."""
    v1 = kubernetes.client.CoreV1Api()
    pvc = v1.read_namespaced_persistent_volume_claim(pvc_name, namespace)
    access_mode = (pvc.spec.access_modes or ["ReadWriteOnce"])[0]
    storage = pvc.spec.resources.requests["storage"]
    return access_mode, storage


def _create_volume_snapshot_sync(name: str, namespace: str, pvc: str, snapshot_class: str) -> None:
    custom = kubernetes.client.CustomObjectsApi()
    body = {
        "apiVersion": f"{_SNAPSHOT_GROUP}/{_SNAPSHOT_VERSION}",
        "kind": "VolumeSnapshot",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "volumeSnapshotClassName": snapshot_class,
            "source": {"persistentVolumeClaimName": pvc},
        },
    }
    custom.create_namespaced_custom_object(
        _SNAPSHOT_GROUP, _SNAPSHOT_VERSION, namespace, _SNAPSHOT_PLURAL, body
    )


def _wait_snapshot_ready_sync(name: str, namespace: str) -> None:
    custom = kubernetes.client.CustomObjectsApi()
    deadline = time.monotonic() + _SNAPSHOT_READY_TIMEOUT
    while time.monotonic() < deadline:
        obj = custom.get_namespaced_custom_object(
            _SNAPSHOT_GROUP, _SNAPSHOT_VERSION, namespace, _SNAPSHOT_PLURAL, name
        )
        if obj.get("status", {}).get("readyToUse"):
            return
        time.sleep(5)
    raise TimeoutError(f"VolumeSnapshot {name} not ready after {_SNAPSHOT_READY_TIMEOUT}s")


def _create_pvc_from_snapshot_sync(
    pvc_name: str,
    namespace: str,
    snapshot_name: str,
    access_mode: str,
    storage: str,
) -> None:
    v1 = kubernetes.client.CoreV1Api()
    body = kubernetes.client.V1PersistentVolumeClaim(
        metadata=kubernetes.client.V1ObjectMeta(name=pvc_name, namespace=namespace),
        spec=kubernetes.client.V1PersistentVolumeClaimSpec(
            access_modes=[access_mode],
            storage_class_name=_EPHEMERAL_STORAGE_CLASS,
            resources=kubernetes.client.V1VolumeResourceRequirements(
                requests={"storage": storage}
            ),
            data_source=kubernetes.client.V1TypedLocalObjectReference(
                api_group=_SNAPSHOT_GROUP,
                kind="VolumeSnapshot",
                name=snapshot_name,
            ),
        ),
    )
    v1.create_namespaced_persistent_volume_claim(namespace, body)


def _wait_pvc_bound_sync(pvc_name: str, namespace: str) -> None:
    v1 = kubernetes.client.CoreV1Api()
    deadline = time.monotonic() + _PVC_BOUND_TIMEOUT
    while time.monotonic() < deadline:
        pvc = v1.read_namespaced_persistent_volume_claim(pvc_name, namespace)
        if pvc.status and pvc.status.phase == "Bound":
            return
        time.sleep(5)
    raise TimeoutError(f"PVC {pvc_name} not bound after {_PVC_BOUND_TIMEOUT}s")


def _delete_pvc_sync(pvc_name: str, namespace: str) -> None:
    v1 = kubernetes.client.CoreV1Api()
    try:
        pvc = v1.read_namespaced_persistent_volume_claim(pvc_name, namespace)
        pv_name = pvc.spec.volume_name
        v1.delete_namespaced_persistent_volume_claim(pvc_name, namespace)
        # For Retain-policy storage classes, explicitly delete the orphaned PV.
        if pv_name:
            try:
                v1.delete_persistent_volume(pv_name)
            except kubernetes.client.exceptions.ApiException:
                pass
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 404:
            raise


def _delete_volume_snapshot_sync(name: str, namespace: str) -> None:
    custom = kubernetes.client.CustomObjectsApi()
    try:
        custom.delete_namespaced_custom_object(
            _SNAPSHOT_GROUP, _SNAPSHOT_VERSION, namespace, _SNAPSHOT_PLURAL, name
        )
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 404:
            raise


async def create_snapshot(name: str, namespace: str, pvc: str, snapshot_class: str) -> None:
    log.info("Creating VolumeSnapshot %s from PVC %s/%s", name, namespace, pvc)
    await asyncio.to_thread(_create_volume_snapshot_sync, name, namespace, pvc, snapshot_class)
    log.info("Waiting for VolumeSnapshot %s to be ready", name)
    await asyncio.to_thread(_wait_snapshot_ready_sync, name, namespace)
    log.info("VolumeSnapshot %s is ready", name)


async def create_pvc_from_snapshot(
    pvc_name: str, namespace: str, snapshot_name: str, source_pvc: str
) -> None:
    access_mode, storage = await asyncio.to_thread(
        _get_pvc_info_sync, source_pvc, namespace
    )
    log.info(
        "Creating ephemeral PVC %s from snapshot %s (%s %s %s)",
        pvc_name, snapshot_name, _EPHEMERAL_STORAGE_CLASS, access_mode, storage,
    )
    await asyncio.to_thread(
        _create_pvc_from_snapshot_sync,
        pvc_name, namespace, snapshot_name, access_mode, storage,
    )
    # WaitForFirstConsumer: PVC stays Pending until the backup Job pod is scheduled.
    # Longhorn then creates the volume on the same node as the pod. No wait here.
    log.info("Ephemeral PVC %s created (pending pod scheduling)", pvc_name)


async def delete_snapshot_and_pvc(
    namespace: str, snapshot_name: str, pvc_name: str | None
) -> None:
    if pvc_name:
        log.info("Deleting ephemeral PVC %s", pvc_name)
        await asyncio.to_thread(_delete_pvc_sync, pvc_name, namespace)
    log.info("Deleting VolumeSnapshot %s", snapshot_name)
    await asyncio.to_thread(_delete_volume_snapshot_sync, snapshot_name, namespace)
