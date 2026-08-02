"""End-to-end: restore guard paths — skip when data present, fail when required and unreachable."""

import base64
import logging

import kubernetes.client

from e2e.conftest import STORAGE_CLASS
from e2e.helpers import wait_init_container_failed, wait_pod_phase

log = logging.getLogger(__name__)


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _make_pvc(v1, ns: str, name: str) -> None:
    v1.create_namespaced_persistent_volume_claim(
        ns,
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": name, "namespace": ns},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": STORAGE_CLASS,
                "resources": {"requests": {"storage": "100Mi"}},
            },
        },
    )


def _make_restore_pod(
    ns: str,
    pod_name: str,
    pvc_name: str,
    secret_name: str,
    k8si_image: str,
    extra_env: list | None = None,
) -> dict:
    env = [
        {"name": "MODE", "value": "restore"},
        {"name": "RESTORE_SENTINELS", "value": "sentinel-file"},
        {
            "name": "RESTIC_REPOSITORY",
            "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "RESTIC_REPOSITORY"}},
        },
        {
            "name": "RESTIC_PASSWORD",
            "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "RESTIC_PASSWORD"}},
        },
    ]
    if extra_env:
        env.extend(extra_env)

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod_name, "namespace": ns},
        "spec": {
            "nodeSelector": {"kubernetes.io/hostname": "hoeve-worker01"},
            "restartPolicy": "Never",
            "volumes": [
                {"name": "data", "persistentVolumeClaim": {"claimName": pvc_name}},
            ],
            "initContainers": [
                {
                    "name": "k8si-restore",
                    "image": k8si_image,
                    "securityContext": {"runAsUser": 0, "runAsGroup": 0},
                    "env": env,
                    "volumeMounts": [
                        {"name": "data", "mountPath": "/data"},
                    ],
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "64Mi"},
                        "limits": {"cpu": "200m", "memory": "256Mi"},
                    },
                }
            ],
            "containers": [
                {
                    "name": "main",
                    "image": "busybox:1.37.0",
                    "command": ["sh", "-c", "sleep 86400"],
                    "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "16Mi"},
                        "limits": {"cpu": "50m", "memory": "32Mi"},
                    },
                }
            ],
        },
    }


def test_restore_required_fails_when_repo_unreachable(ns, k8si_image):
    """Init container must exit non-zero when RESTORE_REQUIRED=true and repo is unreachable."""
    v1 = kubernetes.client.CoreV1Api()

    pvc_name = "guard-required-pvc"
    _make_pvc(v1, ns, pvc_name)

    secret_name = "guard-required-secret"
    v1.create_namespaced_secret(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": secret_name, "namespace": ns},
            "data": {
                "RESTIC_REPOSITORY": _b64("rest:http://nowhere-invalid.svc:8000/"),
                "RESTIC_PASSWORD": _b64("e2etest"),
            },
        },
    )

    pod_name = "guard-required-pod"
    v1.create_namespaced_pod(
        ns,
        _make_restore_pod(
            ns,
            pod_name,
            pvc_name,
            secret_name,
            k8si_image,
            extra_env=[{"name": "RESTORE_REQUIRED", "value": "true"}],
        ),
    )

    exit_code = wait_init_container_failed(ns, pod_name, timeout=120)
    assert exit_code != 0, f"Expected non-zero exit, got {exit_code}"
    log.info("Init container failed with exit code %d as expected", exit_code)


def test_restore_skips_when_sentinel_present(ns, k8si_image):
    """Init container must exit 0 when the sentinel file is already on the PVC."""
    v1 = kubernetes.client.CoreV1Api()

    pvc_name = "guard-sentinel-pvc"
    _make_pvc(v1, ns, pvc_name)

    # Write sentinel via a setup pod before the restore pod mounts the PVC
    setup_name = "guard-sentinel-setup"
    v1.create_namespaced_pod(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": setup_name, "namespace": ns},
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": "hoeve-worker01"},
                "restartPolicy": "Never",
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": pvc_name}}],
                "containers": [
                    {
                        "name": "writer",
                        "image": "busybox:1.37.0",
                        "command": ["sh", "-c", "touch /data/sentinel-file && echo done"],
                        "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                        "resources": {
                            "requests": {"cpu": "10m", "memory": "16Mi"},
                            "limits": {"cpu": "50m", "memory": "32Mi"},
                        },
                    }
                ],
            },
        },
    )
    wait_pod_phase(ns, setup_name, "Succeeded", timeout=60)
    log.info("Sentinel written to PVC")

    secret_name = "guard-sentinel-secret"
    v1.create_namespaced_secret(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": secret_name, "namespace": ns},
            "data": {
                "RESTIC_REPOSITORY": _b64("rest:http://nowhere-invalid.svc:8000/"),
                "RESTIC_PASSWORD": _b64("e2etest"),
            },
        },
    )

    pod_name = "guard-sentinel-pod"
    v1.create_namespaced_pod(ns, _make_restore_pod(ns, pod_name, pvc_name, secret_name, k8si_image))

    # Sentinel is present → init container skips restore → main container starts
    wait_pod_phase(ns, pod_name, "Running", timeout=120)
    log.info("Pod reached Running — init container correctly skipped restore")


def test_restore_skips_with_no_restore_marker(ns, k8si_image):
    """Init container must exit 0 when .k8si-no-restore file is present on the PVC."""
    v1 = kubernetes.client.CoreV1Api()

    pvc_name = "guard-nofile-pvc"
    _make_pvc(v1, ns, pvc_name)

    setup_name = "guard-nofile-setup"
    v1.create_namespaced_pod(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": setup_name, "namespace": ns},
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": "hoeve-worker01"},
                "restartPolicy": "Never",
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": pvc_name}}],
                "containers": [
                    {
                        "name": "writer",
                        "image": "busybox:1.37.0",
                        "command": ["sh", "-c", "touch /data/.k8si-no-restore && echo done"],
                        "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                        "resources": {
                            "requests": {"cpu": "10m", "memory": "16Mi"},
                            "limits": {"cpu": "50m", "memory": "32Mi"},
                        },
                    }
                ],
            },
        },
    )
    wait_pod_phase(ns, setup_name, "Succeeded", timeout=60)
    log.info(".k8si-no-restore written to PVC")

    secret_name = "guard-nofile-secret"
    v1.create_namespaced_secret(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": secret_name, "namespace": ns},
            "data": {
                "RESTIC_REPOSITORY": _b64("rest:http://nowhere-invalid.svc:8000/"),
                "RESTIC_PASSWORD": _b64("e2etest"),
            },
        },
    )

    pod_name = "guard-nofile-pod"
    v1.create_namespaced_pod(ns, _make_restore_pod(ns, pod_name, pvc_name, secret_name, k8si_image))

    wait_pod_phase(ns, pod_name, "Running", timeout=120)
    log.info("Pod reached Running — init container correctly skipped due to .k8si-no-restore")
