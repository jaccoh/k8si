"""Shared wait/cleanup helpers for e2e tests."""

import logging
import subprocess
import time

import kubernetes.client
import kubernetes.client.exceptions

log = logging.getLogger(__name__)


def wait_pod_phase(ns: str, pod_name: str, phase: str, timeout: int = 180) -> None:
    v1 = kubernetes.client.CoreV1Api()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            pod = v1.read_namespaced_pod(pod_name, ns)
            if pod.status and pod.status.phase == phase:
                return
        except kubernetes.client.exceptions.ApiException as e:
            if e.status != 404:
                raise
        time.sleep(3)
    raise TimeoutError(f"Pod {ns}/{pod_name} did not reach phase {phase!r} within {timeout}s")


def wait_pod_deleted(ns: str, pod_name: str, timeout: int = 60) -> None:
    v1 = kubernetes.client.CoreV1Api()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            v1.read_namespaced_pod(pod_name, ns)
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                return
            raise
        time.sleep(3)
    raise TimeoutError(f"Pod {ns}/{pod_name} was not deleted within {timeout}s")


def delete_pvc_with_cleanup(ns: str, pvc_name: str) -> None:
    v1 = kubernetes.client.CoreV1Api()

    try:
        pvc = v1.read_namespaced_persistent_volume_claim(pvc_name, ns)
        pv_name = pvc.spec.volume_name
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            return
        raise

    if pv_name:
        subprocess.run(
            [
                "kubectl", "delete", "lvmsnapshot",
                "-n", "openebs",
                "-l", f"openebs.io/persistent-volume={pv_name}",
                "--ignore-not-found",
            ],
            check=True,
            timeout=60,
        )
        log.info("Cleaned up LVMSnapshot CRs for PV %s", pv_name)

    v1.delete_namespaced_persistent_volume_claim(pvc_name, ns)
    log.info("Deleted PVC %s/%s, waiting for removal", ns, pvc_name)

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            v1.read_namespaced_persistent_volume_claim(pvc_name, ns)
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                log.info("PVC %s/%s gone", ns, pvc_name)
                return
            raise
        time.sleep(3)
    raise TimeoutError(f"PVC {ns}/{pvc_name} was not deleted within 120s")
