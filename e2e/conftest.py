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


def _detect_environment() -> tuple[str, str, str]:
    """Detect cluster nodes and return (node_name, storage_class, snapshot_class)."""
    try:
        import kubernetes.client
        import kubernetes.config

        try:
            kubernetes.config.load_incluster_config()
        except kubernetes.config.ConfigException:
            kubernetes.config.load_kube_config()
        v1 = kubernetes.client.CoreV1Api()
        nodes = [n.metadata.name for n in v1.list_node().items]
        if "orbstack" in nodes:
            return "orbstack", "local-path", "local-path-snapclass"

        storage_api = kubernetes.client.StorageV1Api()
        scs = [sc.metadata.name for sc in storage_api.list_storage_class().items]

        custom_api = kubernetes.client.CustomObjectsApi()
        try:
            snaps = custom_api.list_cluster_custom_object(
                "snapshot.storage.k8s.io", "v1", "volumesnapshotclasses"
            )
            vscs = [v["metadata"]["name"] for v in snaps.get("items", [])]
        except Exception:
            vscs = []

        sc_name = "linstor-worker-local"
        for candidate in ["linstor-worker-local", "linstor-worker-replicated", "local-path"]:
            if candidate in scs:
                sc_name = candidate
                break
        if not sc_name and scs:
            sc_name = scs[0]

        vsc_name = "linstor-snapclass"
        for candidate in ["linstor-snapclass", "local-path-snapclass"]:
            if candidate in vscs:
                vsc_name = candidate
                break
        if not vsc_name and vscs:
            vsc_name = vscs[0]

        return "hoeve-worker01", sc_name, vsc_name
    except Exception:
        pass
    return "hoeve-worker01", "linstor-worker-local", "linstor-snapclass"


NODE_NAME, STORAGE_CLASS, SNAPSHOT_CLASS = _detect_environment()


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
    if NODE_NAME == "orbstack":
        return "k8si:dev"
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
                log.info("Cleaning up PV %s in %s", pv_name, namespace)
    except Exception:
        log.exception("Error listing PVCs during teardown")

    v1.delete_namespace(namespace)
    log.info("Deleted namespace %s", namespace)


@pytest.fixture(scope="module")
def repo_pvc(ns):
    """Create a PVC that backup jobs mount at /repo as the repository (file:// backend)."""
    pvc_name = "e2e-repo-data"
    v1 = kubernetes.client.CoreV1Api()
    v1.create_namespaced_persistent_volume_claim(
        ns,
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": pvc_name, "namespace": ns},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": STORAGE_CLASS,
                "resources": {"requests": {"storage": "512Mi"}},
            },
        },
    )
    log.info("Created repo PVC %s/%s", ns, pvc_name)
    return pvc_name


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
                "storageClassName": STORAGE_CLASS,
                "resources": {"requests": {"storage": "128Mi"}},
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
                "storageClassName": STORAGE_CLASS,
                "resources": {"requests": {"storage": "512Mi"}},
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
                "nodeSelector": {"kubernetes.io/hostname": NODE_NAME},
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
                                    "healthcheck.sh",
                                    "--connect",
                                    "--innodb_initialized",
                                ],
                            },
                            "initialDelaySeconds": 10,
                            "periodSeconds": 5,
                            "failureThreshold": 24,
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

    # The readiness probe passes via the healthcheck user. Wait until root password
    # auth also works — the Docker init SQL may still be running after innodb_initialized.
    deadline = time.monotonic() + 60
    auth_ok = False
    while time.monotonic() < deadline:
        r = subprocess.run(
            [
                "kubectl",
                "exec",
                "mariadb",
                "-n",
                ns,
                "--",
                "mariadb",
                "-u",
                "root",
                f"-p{_MARIADB_ROOT_PASSWORD}",
                _MARIADB_DATABASE,
                "-e",
                "SELECT 1",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if r.returncode == 0:
            auth_ok = True
            break
        log.debug("MariaDB root auth not ready yet: %s", r.stderr.strip())
        time.sleep(3)
    if not auth_ok:
        raise RuntimeError(f"MariaDB root auth failed after 60s: {r.stderr!r}")

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
                "storageClassName": STORAGE_CLASS,
                "resources": {"requests": {"storage": "512Mi"}},
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
                "nodeSelector": {"kubernetes.io/hostname": NODE_NAME},
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
                            "exec": {
                                "command": [
                                    "psql",
                                    "-U",
                                    "postgres",
                                    "-d",
                                    "testdb",
                                    "-c",
                                    "SELECT 1",
                                ]
                            },
                            "initialDelaySeconds": 5,
                            "periodSeconds": 5,
                            "failureThreshold": 24,
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
