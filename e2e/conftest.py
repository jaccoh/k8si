"""Module-scoped fixtures for e2e tests."""

import logging
import subprocess
import time

import pytest
import kubernetes.client
import kubernetes.client.exceptions
import kubernetes.config

from e2e.helpers import wait_pod_phase

log = logging.getLogger(__name__)


def pytest_configure(config):
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()


@pytest.fixture(scope="module")
def ns():
    ts = int(time.time())
    namespace = f"k8si-e2e-{ts}"
    v1 = kubernetes.client.CoreV1Api()
    v1.create_namespace({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}})
    log.info("Created namespace %s", namespace)

    yield namespace

    log.info("Tearing down namespace %s", namespace)
    try:
        pvcs = v1.list_namespaced_persistent_volume_claim(namespace)
        for pvc in pvcs.items:
            pv_name = pvc.spec.volume_name
            if pv_name:
                subprocess.run(
                    [
                        "kubectl", "delete", "lvmsnapshot",
                        "-n", "openebs",
                        "-l", f"openebs.io/persistent-volume={pv_name}",
                        "--ignore-not-found",
                    ],
                    check=False,
                    timeout=60,
                )
    except Exception:
        log.exception("Failed to clean up LVMSnapshot CRs during teardown")

    v1.delete_namespace(namespace)
    log.info("Deleted namespace %s", namespace)


@pytest.fixture(scope="module")
def rest_server_url(ns):
    v1 = kubernetes.client.CoreV1Api()

    pvc_name = "restic-rest-data"
    v1.create_namespaced_persistent_volume_claim(
        ns,
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": pvc_name, "namespace": ns},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": "openebs-lvm-worker-thin",
                "resources": {"requests": {"storage": "500Mi"}},
            },
        },
    )

    pod_name = "restic-rest"
    v1.create_namespaced_pod(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": ns, "labels": {"app": "restic-rest"}},
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": "hoeve-worker01"},
                "restartPolicy": "Always",
                "volumes": [
                    {"name": "data", "persistentVolumeClaim": {"claimName": pvc_name}},
                ],
                "containers": [{
                    "name": "rest-server",
                    "image": "restic/rest-server:latest",
                    "args": ["--no-auth", "--path", "/data"],
                    "ports": [{"containerPort": 8000}],
                    "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "64Mi"},
                        "limits": {"cpu": "200m", "memory": "256Mi"},
                    },
                }],
            },
        },
    )

    svc_name = "restic-rest"
    v1.create_namespaced_service(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": svc_name, "namespace": ns},
            "spec": {
                "type": "ClusterIP",
                "selector": {"app": "restic-rest"},
                "ports": [{"port": 8000, "targetPort": 8000}],
            },
        },
    )

    wait_pod_phase(ns, pod_name, "Running", timeout=180)
    log.info("restic rest-server running in %s", ns)

    return f"rest:http://restic-rest.{ns}.svc.cluster.local:8000/"


@pytest.fixture(scope="module")
def data_pvc(ns):
    ts = int(time.time())
    pvc_name = f"e2e-data-{ts}"
    v1 = kubernetes.client.CoreV1Api()
    v1.create_namespaced_persistent_volume_claim(
        ns,
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": pvc_name, "namespace": ns},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": "openebs-lvm-worker-thin",
                "resources": {"requests": {"storage": "100Mi"}},
            },
        },
    )
    log.info("Created data PVC %s/%s", ns, pvc_name)
    return pvc_name
