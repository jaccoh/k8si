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
_SNAPSHOT_CONFLICT_TIMEOUT = 1800
_JOB_GONE_TIMEOUT = 120


def _wait_no_snapshot_in_progress_sync(pvc_name: str, namespace: str) -> None:
    """Block until no unready VolumeSnapshot targeting pvc_name exists in namespace."""
    custom = kubernetes.client.CustomObjectsApi()
    deadline = time.monotonic() + _SNAPSHOT_CONFLICT_TIMEOUT
    warned = False
    while True:
        items = custom.list_namespaced_custom_object(
            _SNAPSHOT_GROUP, _SNAPSHOT_VERSION, namespace, _SNAPSHOT_PLURAL
        ).get("items", [])
        in_progress = [
            obj
            for obj in items
            if obj.get("spec", {}).get("source", {}).get("persistentVolumeClaimName") == pvc_name
            and not obj.get("status", {}).get("readyToUse")
        ]
        if not in_progress:
            return
        if not warned:
            log.warning(
                "VolumeSnapshot conflict: %d unready snapshot(s) for PVC %s/%s — waiting",
                len(in_progress),
                namespace,
                pvc_name,
            )
            warned = True
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for in-progress VolumeSnapshot(s) on PVC {pvc_name} "
                f"to clear after {_SNAPSHOT_CONFLICT_TIMEOUT}s"
            )
        time.sleep(60)


def _get_pvc_info_sync(pvc_name: str, snapshot_name: str, namespace: str) -> tuple[str, str, str]:
    """Return (accessMode, storage, storageClass), respecting snapshot restoreSize."""
    v1 = kubernetes.client.CoreV1Api()
    custom = kubernetes.client.CustomObjectsApi()
    pvc = v1.read_namespaced_persistent_volume_claim(pvc_name, namespace)
    access_mode = (pvc.spec.access_modes or ["ReadWriteOnce"])[0]
    storage = pvc.spec.resources.requests["storage"]
    storage_class = pvc.spec.storage_class_name or ""

    try:
        snap = custom.get_namespaced_custom_object(
            _SNAPSHOT_GROUP, _SNAPSHOT_VERSION, namespace, _SNAPSHOT_PLURAL, snapshot_name
        )
        restore_size = snap.get("status", {}).get("restoreSize")
        if restore_size:
            storage = str(restore_size)
    except Exception as e:
        log.warning("Could not fetch restoreSize for VolumeSnapshot %s: %s", snapshot_name, e)

    return access_mode, storage, storage_class


def _create_volume_snapshot_sync(
    name: str, namespace: str, pvc: str, snapshot_class: str | None
) -> None:
    custom = kubernetes.client.CustomObjectsApi()
    spec: dict = {"source": {"persistentVolumeClaimName": pvc}}
    if snapshot_class:
        spec["volumeSnapshotClassName"] = snapshot_class
    body = {
        "apiVersion": f"{_SNAPSHOT_GROUP}/{_SNAPSHOT_VERSION}",
        "kind": "VolumeSnapshot",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }
    custom.create_namespaced_custom_object(
        _SNAPSHOT_GROUP, _SNAPSHOT_VERSION, namespace, _SNAPSHOT_PLURAL, body
    )


def _wait_snapshot_ready_sync(name: str, namespace: str) -> None:
    custom = kubernetes.client.CustomObjectsApi()
    deadline = time.monotonic() + _SNAPSHOT_READY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            obj = custom.get_namespaced_custom_object(
                _SNAPSHOT_GROUP, _SNAPSHOT_VERSION, namespace, _SNAPSHOT_PLURAL, name
            )
        except kubernetes.client.exceptions.ApiException as e:
            if e.status >= 500:
                log.warning(
                    "Transient K8s API error (HTTP %s) waiting for VolumeSnapshot %s; retrying",
                    e.status,
                    name,
                )
                time.sleep(5)
                continue
            raise
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
    storage_class: str,
) -> None:
    v1 = kubernetes.client.CoreV1Api()
    body = kubernetes.client.V1PersistentVolumeClaim(
        metadata=kubernetes.client.V1ObjectMeta(name=pvc_name, namespace=namespace),
        spec=kubernetes.client.V1PersistentVolumeClaimSpec(
            access_modes=[access_mode],
            storage_class_name=storage_class,
            resources=kubernetes.client.V1VolumeResourceRequirements(requests={"storage": storage}),
            data_source=kubernetes.client.V1TypedLocalObjectReference(
                api_group=_SNAPSHOT_GROUP,
                kind="VolumeSnapshot",
                name=snapshot_name,
            ),
        ),
    )
    v1.create_namespaced_persistent_volume_claim(namespace, body)


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


async def create_snapshot(name: str, namespace: str, pvc: str, snapshot_class: str | None) -> None:
    await asyncio.to_thread(_wait_no_snapshot_in_progress_sync, pvc, namespace)
    log.info(
        "Creating VolumeSnapshot %s from PVC %s/%s (class=%s)",
        name,
        namespace,
        pvc,
        snapshot_class or "<cluster-default>",
    )
    await asyncio.to_thread(_create_volume_snapshot_sync, name, namespace, pvc, snapshot_class)
    log.info("Waiting for VolumeSnapshot %s to be ready", name)
    await asyncio.to_thread(_wait_snapshot_ready_sync, name, namespace)
    log.info("VolumeSnapshot %s is ready", name)


async def create_pvc_from_snapshot(
    pvc_name: str, namespace: str, snapshot_name: str, source_pvc: str
) -> None:
    access_mode, storage, storage_class = await asyncio.to_thread(
        _get_pvc_info_sync, source_pvc, snapshot_name, namespace
    )
    log.info(
        "Creating ephemeral PVC %s from snapshot %s (sc=%s %s %s)",
        pvc_name,
        snapshot_name,
        storage_class,
        access_mode,
        storage,
    )
    await asyncio.to_thread(
        _create_pvc_from_snapshot_sync,
        pvc_name,
        namespace,
        snapshot_name,
        access_mode,
        storage,
        storage_class,
    )
    log.info("Ephemeral PVC %s created (pending pod scheduling)", pvc_name)


async def delete_snapshot_and_pvc(namespace: str, snapshot_name: str, pvc_name: str | None) -> None:
    if pvc_name:
        log.info("Deleting ephemeral PVC %s", pvc_name)
        await asyncio.to_thread(_delete_pvc_sync, pvc_name, namespace)
    log.info("Deleting VolumeSnapshot %s", snapshot_name)
    await asyncio.to_thread(_delete_volume_snapshot_sync, snapshot_name, namespace)
