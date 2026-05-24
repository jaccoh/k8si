"""Module-scoped fixtures for e2e tests."""

import base64
import logging
import os
import subprocess
import time

import kubernetes.client
import kubernetes.client.exceptions
import kubernetes.config
import pytest

from e2e.helpers import wait_pod_condition, wait_pod_phase

log = logging.getLogger(__name__)

_MARIADB_ROOT_PASSWORD = "e2etest"
_MARIADB_DATABASE = "testdb"
_POSTGRES_PASSWORD = "e2etest"
_POSTGRES_DB = "testdb"


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def pytest_configure(config):
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()


@pytest.fixture(scope="session")
def k8si_image():
    tag = os.environ.get("IMAGE_TAG")
    if tag:
        return f"docker.hoeve.nu/k8si:{tag}"
    return "ghcr.io/jaccoh/k8si:latest"


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
                        "kubectl",
                        "delete",
                        "lvmsnapshot",
                        "-n",
                        "openebs",
                        "-l",
                        f"openebs.io/persistent-volume={pv_name}",
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
                "containers": [
                    {
                        "name": "rest-server",
                        "image": "restic/rest-server:latest",
                        "command": ["/usr/bin/rest-server"],
                        "args": ["--no-auth", "--path", "/data"],
                        "ports": [{"containerPort": 8000}],
                        "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "64Mi"},
                            "limits": {"cpu": "200m", "memory": "256Mi"},
                        },
                    }
                ],
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


@pytest.fixture(scope="module")
def mariadb_env(ns):
    """Spin up MariaDB 11 pod + PVC + Service + credentials Secret.

    Yields (pvc_name, creds_secret_name). The PVC contains the MariaDB data
    directory; the test is responsible for deleting the pod before deleting the
    PVC so the volume can be unbound.
    """
    v1 = kubernetes.client.CoreV1Api()

    pvc_name = "e2e-mariadb-data"
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

    secret_name = "e2e-mariadb-creds"
    v1.create_namespaced_secret(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": secret_name, "namespace": ns},
            "data": {
                "DB_HOST": _b64("mariadb"),
                "DB_PORT": _b64("3306"),
                "DB_USER": _b64("root"),
                "DB_PASSWORD": _b64(_MARIADB_ROOT_PASSWORD),
                "DB_NAME": _b64(_MARIADB_DATABASE),
            },
        },
    )

    v1.create_namespaced_pod(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "mariadb", "namespace": ns, "labels": {"app": "mariadb"}},
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": "hoeve-worker01"},
                "restartPolicy": "Always",
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": pvc_name}}],
                "containers": [
                    {
                        "name": "mariadb",
                        "image": "mariadb:11",
                        "env": [
                            {"name": "MYSQL_ROOT_PASSWORD", "value": _MARIADB_ROOT_PASSWORD},
                            {"name": "MYSQL_DATABASE", "value": _MARIADB_DATABASE},
                        ],
                        "volumeMounts": [{"name": "data", "mountPath": "/var/lib/mysql"}],
                        "readinessProbe": {
                            "exec": {
                                "command": [
                                    "mysqladmin",
                                    "ping",
                                    "-h",
                                    "127.0.0.1",
                                    f"-p{_MARIADB_ROOT_PASSWORD}",
                                ],
                            },
                            "initialDelaySeconds": 10,
                            "periodSeconds": 5,
                            "failureThreshold": 12,
                        },
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "256Mi"},
                            "limits": {"cpu": "500m", "memory": "512Mi"},
                        },
                    }
                ],
            },
        },
    )

    v1.create_namespaced_service(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "mariadb", "namespace": ns},
            "spec": {
                "type": "ClusterIP",
                "selector": {"app": "mariadb"},
                "ports": [{"port": 3306, "targetPort": 3306}],
            },
        },
    )

    wait_pod_phase(ns, "mariadb", "Running", timeout=300)
    wait_pod_condition(ns, "mariadb", "Ready", timeout=300)
    log.info("MariaDB running and ready in %s", ns)

    yield pvc_name, secret_name


@pytest.fixture(scope="module")
def postgres_env(ns):
    """Spin up Postgres 16 pod + PVC + Service + credentials Secret.

    Uses PGDATA=/var/lib/postgresql/data/pgdata (subdirectory of mount) to avoid
    initdb failure when the mount root already contains filesystem metadata.
    Yields (pvc_name, creds_secret_name).
    """
    v1 = kubernetes.client.CoreV1Api()

    pvc_name = "e2e-postgres-data"
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

    secret_name = "e2e-postgres-creds"
    v1.create_namespaced_secret(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": secret_name, "namespace": ns},
            "data": {
                "DB_HOST": _b64("postgres"),
                "DB_PORT": _b64("5432"),
                "DB_USER": _b64("postgres"),
                "DB_PASSWORD": _b64(_POSTGRES_PASSWORD),
                "DB_NAME": _b64(_POSTGRES_DB),
            },
        },
    )

    v1.create_namespaced_pod(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "postgres", "namespace": ns, "labels": {"app": "postgres"}},
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": "hoeve-worker01"},
                "restartPolicy": "Always",
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": pvc_name}}],
                "containers": [
                    {
                        "name": "postgres",
                        "image": "postgres:16",
                        "env": [
                            {"name": "POSTGRES_PASSWORD", "value": _POSTGRES_PASSWORD},
                            {"name": "POSTGRES_DB", "value": _POSTGRES_DB},
                            {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
                        ],
                        "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql/data"}],
                        "readinessProbe": {
                            "exec": {"command": ["pg_isready", "-U", "postgres"]},
                            "initialDelaySeconds": 5,
                            "periodSeconds": 5,
                            "failureThreshold": 12,
                        },
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "256Mi"},
                            "limits": {"cpu": "500m", "memory": "512Mi"},
                        },
                    }
                ],
            },
        },
    )

    v1.create_namespaced_service(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "postgres", "namespace": ns},
            "spec": {
                "type": "ClusterIP",
                "selector": {"app": "postgres"},
                "ports": [{"port": 5432, "targetPort": 5432}],
            },
        },
    )

    wait_pod_phase(ns, "postgres", "Running", timeout=300)
    wait_pod_condition(ns, "postgres", "Ready", timeout=120)
    log.info("Postgres running and ready in %s", ns)

    yield pvc_name, secret_name
