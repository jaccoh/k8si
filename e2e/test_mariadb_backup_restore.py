"""End-to-end: MariaDB FTWRL quiesce, backup, and restore via k8si init container."""

import asyncio
import base64
import logging
import subprocess
import uuid

import kubernetes.client

from e2e.conftest import SNAPSHOT_CLASS, STORAGE_CLASS, _MARIADB_DATABASE, _MARIADB_ROOT_PASSWORD
from e2e.helpers import (
    delete_pvc_with_cleanup,
    wait_init_container_failed,
    wait_pod_condition,
    wait_pod_deleted,
    wait_pod_phase,
)
from k8si.operator.workflow import run_backup

log = logging.getLogger(__name__)

KNOWN_VALUE = f"e2e-{uuid.uuid4().hex[:12]}"
_MARIADB_ROOT_PASSWORD = "e2etest"
_MARIADB_DATABASE = "testdb"


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _mysql_exec(ns: str, pod_name: str, sql: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [
            "kubectl",
            "exec",
            pod_name,
            "-n",
            ns,
            "--",
            "mariadb",
            "-u",
            "root",
            f"-p{_MARIADB_ROOT_PASSWORD}",
            _MARIADB_DATABASE,
            "-e",
            sql,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"mysql exec failed (rc={result.returncode})\n"
            f"stdout: {result.stdout!r}\n"
            f"stderr: {result.stderr!r}"
        )
    return result


def test_mariadb_backup_and_restore(ns, repo_pvc, mariadb_env, k8si_image):
    pvc_name, creds_secret = mariadb_env
    v1 = kubernetes.client.CoreV1Api()

    _mysql_exec(
        ns,
        "mariadb",
        "CREATE TABLE IF NOT EXISTS items (v VARCHAR(255));"
        f" INSERT INTO items VALUES ('{KNOWN_VALUE}');",
    )
    log.info("Inserted KNOWN_VALUE=%s into MariaDB", KNOWN_VALUE)

    restic_secret_name = "e2e-restic-mariadb"
    v1.create_namespaced_secret(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": restic_secret_name, "namespace": ns},
            "data": {
                "RESTIC_REPOSITORY": _b64("local:/repo"),
                "RESTIC_PASSWORD": _b64("e2etest"),
            },
        },
    )

    spec = {
        "pvc": pvc_name,
        "resticSecret": restic_secret_name,
        "repositoryPVC": repo_pvc,
        "schedule": "0 0 1 1 *",
        "volumeSnapshotClass": SNAPSHOT_CLASS,
        "database": {
            "type": "mariadb",
            "secretRef": creds_secret,
        },
        "restore": {"sentinels": ["ibdata1"]},
    }

    result = asyncio.run(run_backup("e2e-mariadb", ns, spec, log))
    assert result["lastBackupResult"] == "success", f"Unexpected result: {result}"
    log.info("MariaDB backup succeeded: %s", result)

    v1.delete_namespaced_pod("mariadb", ns)
    wait_pod_deleted(ns, "mariadb", timeout=60)

    delete_pvc_with_cleanup(ns, pvc_name)

    v1.create_namespaced_persistent_volume_claim(
        ns,
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": pvc_name, "namespace": ns},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": STORAGE_CLASS,
                "resources": {"requests": {"storage": "500Mi"}},
            },
        },
    )
    log.info("Created fresh empty PVC %s/%s for restore", ns, pvc_name)

    verifier_name = "mariadb-restore-verifier"
    v1.create_namespaced_pod(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": verifier_name, "namespace": ns},
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": "hoeve-worker01"},
                "restartPolicy": "Never",
                "volumes": [
                    {"name": "data", "persistentVolumeClaim": {"claimName": pvc_name}},
                    {"name": "repo", "persistentVolumeClaim": {"claimName": repo_pvc}},
                ],
                "initContainers": [
                    {
                        "name": "k8si-restore",
                        "image": k8si_image,
                        "securityContext": {"runAsUser": 0, "runAsGroup": 0},
                        "env": [
                            {"name": "MODE", "value": "restore"},
                            {"name": "RESTORE_SENTINELS", "value": "ibdata1"},
                            {
                                "name": "RESTIC_REPOSITORY",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": restic_secret_name,
                                        "key": "RESTIC_REPOSITORY",
                                    },
                                },
                            },
                            {
                                "name": "RESTIC_PASSWORD",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": restic_secret_name,
                                        "key": "RESTIC_PASSWORD",
                                    },
                                },
                            },
                        ],
                        "volumeMounts": [
                            {"name": "data", "mountPath": "/data"},
                            {"name": "repo", "mountPath": "/repo"},
                        ],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "64Mi"},
                            "limits": {"cpu": "200m", "memory": "512Mi"},
                        },
                    }
                ],
                "containers": [
                    {
                        "name": "mariadb",
                        "image": "mariadb:11",
                        "env": [
                            {"name": "MYSQL_ROOT_PASSWORD", "value": _MARIADB_ROOT_PASSWORD},
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
                            "initialDelaySeconds": 15,
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

    wait_pod_phase(ns, verifier_name, "Running", timeout=900)
    wait_pod_condition(ns, verifier_name, "Ready", timeout=180)
    log.info("MariaDB restore verifier pod running and ready")

    proc = _mysql_exec(
        ns,
        verifier_name,
        f"SELECT v FROM items WHERE v='{KNOWN_VALUE}';",
    )
    log.info("Verification stdout: %r", proc.stdout)
    assert KNOWN_VALUE in proc.stdout, f"KNOWN_VALUE not found in query result: {proc.stdout!r}"


def test_mariadb_restore_required_fails_without_backup(ns, k8si_image):
    """Init container must exit non-zero when RESTORE_REQUIRED=true and no restic repo exists."""
    v1 = kubernetes.client.CoreV1Api()

    pvc_name = "e2e-mariadb-guard-pvc"
    v1.create_namespaced_persistent_volume_claim(
        ns,
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": pvc_name, "namespace": ns},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": STORAGE_CLASS,
                "resources": {"requests": {"storage": "100Mi"}},
            },
        },
    )

    secret_name = "e2e-mariadb-guard-secret"
    v1.create_namespaced_secret(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": secret_name, "namespace": ns},
            "data": {
                "RESTIC_REPOSITORY": _b64("rest:http://restic-rest.nowhere.svc:8000/"),
                "RESTIC_PASSWORD": _b64("e2etest"),
            },
        },
    )

    pod_name = "mariadb-guard-pod"
    v1.create_namespaced_pod(
        ns,
        {
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
                        "env": [
                            {"name": "MODE", "value": "restore"},
                            {"name": "RESTORE_SENTINELS", "value": "ibdata1"},
                            {"name": "RESTORE_REQUIRED", "value": "true"},
                            {
                                "name": "RESTIC_REPOSITORY",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": secret_name,
                                        "key": "RESTIC_REPOSITORY",
                                    },
                                },
                            },
                            {
                                "name": "RESTIC_PASSWORD",
                                "valueFrom": {
                                    "secretKeyRef": {"name": secret_name, "key": "RESTIC_PASSWORD"},
                                },
                            },
                        ],
                        "volumeMounts": [
                            {"name": "data", "mountPath": "/data"},
                        ],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "64Mi"},
                            "limits": {"cpu": "200m", "memory": "512Mi"},
                        },
                    }
                ],
                "containers": [
                    {
                        "name": "never-starts",
                        "image": "busybox:1.37.0",
                        "command": ["sh", "-c", "sleep 86400"],
                        "resources": {
                            "requests": {"cpu": "10m", "memory": "16Mi"},
                            "limits": {"cpu": "50m", "memory": "32Mi"},
                        },
                    }
                ],
            },
        },
    )

    exit_code = wait_init_container_failed(ns, pod_name, timeout=120)
    assert exit_code != 0, f"Expected non-zero exit, got {exit_code}"
    log.info("Init container failed with exit code %d as expected", exit_code)
