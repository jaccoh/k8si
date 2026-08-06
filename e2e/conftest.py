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


def _mariadb_healthcheck_command() -> list[str]:
    """healthcheck.sh command for MariaDB readinessProbe.

    --su-mysql re-execs the script as the mysql unix user and must come
    FIRST -- per the script's own docs it "disregards previous options
    set". If it isn't first, --connect runs as the wrong user and fails
    before --su-mysql ever takes effect.
    """
    return ["healthcheck.sh", "--su-mysql", "--connect", "--innodb_initialized"]


def _mariadb_readiness_probe(*, initial_delay_seconds: int = 10) -> dict:
    """readinessProbe dict for the MariaDB pod.

    timeoutSeconds=10 was too tight under CI runner CPU limits: healthcheck.sh
    consistently timed out (23/23 attempts in run 1385), so the probe never
    once succeeded within the 300s wait. periodSeconds matches timeoutSeconds
    so kubelet never queues up overlapping execs on an already-slow probe.
    """
    return {
        "exec": {
            "command": _mariadb_healthcheck_command(),
        },
        "initialDelaySeconds": initial_delay_seconds,
        "periodSeconds": 30,
        "timeoutSeconds": 30,
        "failureThreshold": 24,
    }


def _pick_storage_class(storage_classes: list) -> str | None:
    """Heuristically pick a storageclass for ephemeral test PVCs.

    Prefers the cluster default; otherwise a non-replicated, non-SMB class
    (cheapest/fastest for throwaway e2e data), falling back to whatever
    exists. Returns None if the cluster has no storageclasses at all.
    """
    if not storage_classes:
        return None
    for sc in storage_classes:
        annotations = sc.metadata.annotations or {}
        if annotations.get("storageclass.kubernetes.io/is-default-class") == "true":
            return sc.metadata.name
    candidates = [sc.metadata.name for sc in storage_classes if "smb" not in sc.metadata.name]
    for name in candidates:
        if "replicated" not in name:
            return name
    if candidates:
        return candidates[0]
    return storage_classes[0].metadata.name


def _detect_environment() -> tuple[str, str, str]:
    """Detect cluster nodes and return (node_name, storage_class, snapshot_class).

    Override via E2E_NODE_NAME / E2E_STORAGE_CLASS / E2E_SNAPSHOT_CLASS env
    vars — set explicitly in CI. Storageclass names are cluster-specific and
    change over time (e.g. an openebs -> linstor migration broke a prior
    hardcoded name here); auto-detection is a local-dev convenience only,
    never a substitute for pinning the CI environment.
    """
    env_node = os.environ.get("E2E_NODE_NAME")
    env_sc = os.environ.get("E2E_STORAGE_CLASS")
    env_vsc = os.environ.get("E2E_SNAPSHOT_CLASS")
    if env_node and env_sc and env_vsc:
        return env_node, env_sc, env_vsc

    import kubernetes.client
    import kubernetes.config

    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()
    v1 = kubernetes.client.CoreV1Api()
    nodes = [n.metadata.name for n in v1.list_node().items]
    if "orbstack" in nodes:
        return (
            env_node or "orbstack",
            env_sc or "local-path",
            env_vsc or "local-path-snapclass",
        )

    storage_classes = kubernetes.client.StorageV1Api().list_storage_class().items
    detected_sc = _pick_storage_class(storage_classes)
    if not nodes or not detected_sc:
        raise RuntimeError(
            "Could not auto-detect e2e node/storageclass "
            "(no nodes or no storageclasses found) — "
            "set E2E_NODE_NAME, E2E_STORAGE_CLASS and E2E_SNAPSHOT_CLASS explicitly."
        )

    detected_vsc = env_vsc
    if not detected_vsc:
        custom_api = kubernetes.client.CustomObjectsApi()
        try:
            snaps = custom_api.list_cluster_custom_object(
                "snapshot.storage.k8s.io", "v1", "volumesnapshotclasses"
            )
            vscs = [v["metadata"]["name"] for v in snaps.get("items", [])]
        except Exception:
            vscs = []
        detected_vsc = vscs[0] if vscs else "linstor-snapclass"

    return env_node or nodes[0], env_sc or detected_sc, detected_vsc


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
                        "readinessProbe": _mariadb_readiness_probe(),
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "256Mi"},
                            "limits": {"cpu": "500m", "memory": "768Mi"},
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
                            "limits": {"cpu": "500m", "memory": "768Mi"},
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
