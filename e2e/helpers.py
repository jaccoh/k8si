"""Shared wait/cleanup helpers for e2e tests (XFS StorageClass enabled)."""

import logging
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


def _fmt_container_states(pod: object) -> str:
    """One-line summary of each container's state/restarts, for stall diagnostics."""
    parts = []
    for cs in getattr(pod.status, "container_statuses", None) or []:  # type: ignore[union-attr]
        if cs.state and cs.state.running:
            detail = f"running(restarts={cs.restart_count})"
        elif cs.state and cs.state.waiting:
            detail = f"waiting({cs.state.waiting.reason}, restarts={cs.restart_count}"
            last = cs.last_state.terminated if cs.last_state else None
            if last:
                detail += f", last={last.reason}/{last.exit_code}"
            detail += ")"
        elif cs.state and cs.state.terminated:
            t = cs.state.terminated
            detail = f"terminated({t.reason}/{t.exit_code}, restarts={cs.restart_count})"
        else:
            detail = f"unknown(restarts={cs.restart_count})"
        parts.append(f"{cs.name}={detail}")
    return ", ".join(parts)


def _fmt_recent_events(events: list) -> str:
    """One-line summary of Event objects (e.g. probe failures), for stall diagnostics."""
    return "; ".join(f"{e.reason}(x{e.count}): {e.message}" for e in events)


def _get_pod_events(v1: object, ns: str, pod_name: str) -> str:
    """Fetch Warning events for a pod (e.g. readiness probe failures), best-effort."""
    try:
        events = v1.list_namespaced_event(  # type: ignore[union-attr]
            ns, field_selector=f"involvedObject.name={pod_name},type=Warning"
        ).items
        return _fmt_recent_events(events)
    except Exception:
        return ""


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
    last_log = time.monotonic()
    last_states = ""
    while time.monotonic() < deadline:
        try:
            pod = v1.read_namespaced_pod(pod_name, ns)
            for cond in pod.status.conditions or []:
                if cond.type == condition_type and cond.status == "True":
                    return
            last_states = _fmt_container_states(pod)
            # Log every 30s so CI logs show what's happening during long waits
            if time.monotonic() - last_log >= 30:
                last_log = time.monotonic()
                elapsed = int(timeout - (deadline - time.monotonic()))
                events = _get_pod_events(v1, ns, pod_name)
                log.info(
                    "wait_pod_condition: %s/%s condition=%s not True yet, states=[%s]"
                    " events=[%s] elapsed=%ds",
                    ns,
                    pod_name,
                    condition_type,
                    last_states,
                    events,
                    elapsed,
                )
        except kubernetes.client.exceptions.ApiException as e:
            if e.status != 404:
                raise
        time.sleep(3)
    final_events = _get_pod_events(v1, ns, pod_name)
    raise TimeoutError(
        f"Pod {ns}/{pod_name} condition {condition_type!r} not True within {timeout}s"
        + (f" (states=[{last_states}])" if last_states else "")
        + (f" (events=[{final_events}])" if final_events else "")
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

    # Give Kubelet time to complete unmounting volumes from deleted pods
    time.sleep(10)

    try:
        pvc = v1.read_namespaced_persistent_volume_claim(pvc_name, ns)
        pv_name = pvc.spec.volume_name
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            return
        raise

    if pv_name:
        log.info("PVC %s (PV %s) deleted", pvc_name, pv_name)

    v1.delete_namespaced_persistent_volume_claim(pvc_name, ns)
    log.info("Deleted PVC %s/%s, waiting for removal", ns, pvc_name)

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            v1.read_namespaced_persistent_volume_claim(pvc_name, ns)
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                log.info("PVC %s/%s gone", ns, pvc_name)
                break
            raise
        time.sleep(3)

    if pv_name:
        pv_deadline = time.monotonic() + 60
        while time.monotonic() < pv_deadline:
            try:
                v1.read_persistent_volume(pv_name)
            except kubernetes.client.exceptions.ApiException as e:
                if e.status == 404:
                    log.info("PV %s gone", pv_name)
                    break
                raise
            time.sleep(2)

        storage_v1 = kubernetes.client.StorageV1Api()
        va_deadline = time.monotonic() + 60
        while time.monotonic() < va_deadline:
            try:
                attachments = storage_v1.list_volume_attachment().items
                matching = [
                    va
                    for va in attachments
                    if va.spec
                    and va.spec.source
                    and va.spec.source.persistent_volume_name == pv_name
                ]
                if not matching:
                    log.info("VolumeAttachments for PV %s gone", pv_name)
                    break
            except Exception:
                break
            time.sleep(2)
