"""Shared wait/cleanup helpers for e2e tests."""

import logging
import subprocess
import time

import kubernetes.client
import kubernetes.client.exceptions

log = logging.getLogger(__name__)


def _collect_init_container_logs(v1: object, ns: str, pod_name: str, pod: object) -> str:
    """Return logs from the first failed init container, or empty string."""
    try:
        for cs in getattr(pod.status, "init_container_statuses", None) or []:  # type: ignore[union-attr]
            if cs.state and cs.state.terminated and cs.state.terminated.exit_code != 0:
                return v1.read_namespaced_pod_log(  # type: ignore[union-attr]
                    pod_name, ns, container=cs.name
                )
    except Exception:
        pass
    return ""


def _fmt_init_states(pod: object) -> str:
    parts = []
    for cs in getattr(pod.status, "init_container_statuses", None) or []:  # type: ignore[union-attr]
        if cs.state and cs.state.running:
            parts.append(f"{cs.name}=running")
        elif cs.state and cs.state.waiting:
            parts.append(f"{cs.name}=waiting({cs.state.waiting.reason})")
        elif cs.state and cs.state.terminated:
            parts.append(f"{cs.name}=done(rc={cs.state.terminated.exit_code})")
    return ", ".join(parts)


def wait_pod_phase(ns: str, pod_name: str, phase: str, timeout: int = 180) -> None:
    v1 = kubernetes.client.CoreV1Api()
    deadline = time.monotonic() + timeout
    last_log = time.monotonic()
    while time.monotonic() < deadline:
        try:
            pod = v1.read_namespaced_pod(pod_name, ns)
            cur_phase = (pod.status.phase or "unknown") if pod.status else "unknown"
            if cur_phase == phase:
                # For Running, also verify no container is in a crash/error state
                if phase == "Running":
                    bad = [
                        cs.state.waiting.reason
                        for cs in (pod.status.container_statuses or [])
                        if cs.state
                        and cs.state.waiting
                        and cs.state.waiting.reason
                        in ("CrashLoopBackOff", "RunContainerError", "OOMKilled", "Error")
                    ]
                    if bad:
                        raise RuntimeError(f"Pod {ns}/{pod_name} container error: {bad}")
                return
            # Fast-fail: pod reached a terminal phase we're not waiting for
            if phase == "Running" and cur_phase in ("Failed", "Succeeded"):
                init_states = _fmt_init_states(pod)
                logs = _collect_init_container_logs(v1, ns, pod_name, pod)
                raise RuntimeError(
                    f"Pod {ns}/{pod_name} reached {cur_phase!r} (not Running)"
                    + (f"; inits=[{init_states}]" if init_states else "")
                    + (f"\n--- init container logs ---\n{logs}" if logs else "")
                )
            # Log init-container progress every 30s so CI logs show what's happening
            if time.monotonic() - last_log >= 30:
                last_log = time.monotonic()
                init_states = _fmt_init_states(pod)
                elapsed = int(timeout - (deadline - time.monotonic()))
                log.info(
                    "wait_pod_phase: %s/%s phase=%s inits=[%s] elapsed=%ds",
                    ns,
                    pod_name,
                    cur_phase,
                    init_states,
                    elapsed,
                )
        except kubernetes.client.exceptions.ApiException as e:
            if e.status != 404:
                raise
        time.sleep(3)
    # Collect final pod state for diagnostic message
    diag = ""
    try:
        pod = v1.read_namespaced_pod(pod_name, ns)
        cur_phase = (pod.status.phase or "unknown") if pod.status else "unknown"
        init_states = _fmt_init_states(pod)
        diag = f" (phase={cur_phase}, inits=[{init_states}])"
    except Exception:
        pass
    raise TimeoutError(f"Pod {ns}/{pod_name} did not reach phase {phase!r} within {timeout}s{diag}")


def wait_pod_condition(
    ns: str, pod_name: str, condition_type: str = "Ready", timeout: int = 120
) -> None:
    v1 = kubernetes.client.CoreV1Api()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            pod = v1.read_namespaced_pod(pod_name, ns)
            for cond in pod.status.conditions or []:
                if cond.type == condition_type and cond.status == "True":
                    return
        except kubernetes.client.exceptions.ApiException as e:
            if e.status != 404:
                raise
        time.sleep(3)
    raise TimeoutError(
        f"Pod {ns}/{pod_name} condition {condition_type!r} not True within {timeout}s"
    )


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


def wait_init_container_failed(ns: str, pod_name: str, timeout: int = 120) -> int:
    """Wait until an init container terminates with non-zero exit code. Returns the exit code."""
    v1 = kubernetes.client.CoreV1Api()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            pod = v1.read_namespaced_pod(pod_name, ns)
            for cs in pod.status.init_container_statuses or []:
                if cs.state and cs.state.terminated and cs.state.terminated.exit_code != 0:
                    return cs.state.terminated.exit_code
        except kubernetes.client.exceptions.ApiException as e:
            if e.status != 404:
                raise
        time.sleep(3)
    raise TimeoutError(f"No failing init container in {ns}/{pod_name} within {timeout}s")


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
                "kubectl",
                "delete",
                "lvmsnapshot",
                "-n",
                "openebs",
                "-l",
                f"openebs.io/persistent-volume={pv_name}",
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
