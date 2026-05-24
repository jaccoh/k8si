"""End-to-end: Postgres CHECKPOINT quiesce, backup, and restore via k8si init container."""

import asyncio
import base64
import logging
import subprocess
import uuid

import kubernetes.client

from e2e.helpers import (
    delete_pvc_with_cleanup,
    wait_pod_condition,
    wait_pod_deleted,
    wait_pod_phase,
)
from k8si.operator.workflow import run_backup

log = logging.getLogger(__name__)

KNOWN_VALUE = f"e2e-{uuid.uuid4().hex[:12]}"
_POSTGRES_PASSWORD = "e2etest"
_POSTGRES_DB = "testdb"
# Sentinel: the PG_VERSION file inside the pgdata subdirectory.
# PGDATA=/var/lib/postgresql/data/pgdata → PVC root contains pgdata/PG_VERSION.
# Restic snapshot paths: /data/pgdata/PG_VERSION. Restore puts it at /data/pgdata/PG_VERSION.
_SENTINEL = "pgdata/PG_VERSION"


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _psql_exec(ns: str, pod_name: str, sql: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "kubectl",
            "exec",
            pod_name,
            "-n",
            ns,
            "--",
            "psql",
            "-U",
            "postgres",
            _POSTGRES_DB,
            "-c",
            sql,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )


def test_postgres_backup_and_restore(ns, rest_server_url, postgres_env, k8si_image):
    pvc_name, creds_secret = postgres_env
    v1 = kubernetes.client.CoreV1Api()

    _psql_exec(
        ns,
        "postgres",
        f"CREATE TABLE IF NOT EXISTS items (v TEXT); INSERT INTO items VALUES ('{KNOWN_VALUE}');",
    )
    log.info("Inserted KNOWN_VALUE=%s into Postgres", KNOWN_VALUE)

    restic_secret_name = "e2e-restic-postgres"
    v1.create_namespaced_secret(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": restic_secret_name, "namespace": ns},
            "data": {
                "RESTIC_REPOSITORY": _b64(rest_server_url),
                "RESTIC_PASSWORD": _b64("e2etest"),
                "RESTIC_SFTP_COMMAND": _b64(""),
                "id_ed25519": _b64(""),
                "known_hosts": _b64(""),
            },
        },
    )

    spec = {
        "pvc": pvc_name,
        "resticSecret": restic_secret_name,
        "schedule": "0 0 1 1 *",
        "volumeSnapshotClass": "openebs-lvm-snapclass",
        "database": {
            "type": "postgres",
            "secretRef": creds_secret,
        },
        "restore": {"sentinels": [_SENTINEL]},
    }

    result = asyncio.run(run_backup("e2e-postgres", ns, spec, log))
    assert result["lastBackupResult"] == "success", f"Unexpected result: {result}"
    log.info("Postgres backup succeeded: %s", result)

    v1.delete_namespaced_pod("postgres", ns)
    wait_pod_deleted(ns, "postgres", timeout=60)

    delete_pvc_with_cleanup(ns, pvc_name)

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
    log.info("Created fresh empty PVC %s/%s for restore", ns, pvc_name)

    verifier_name = "postgres-restore-verifier"
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
                    {
                        "name": "restic-ssh",
                        "secret": {
                            "secretName": restic_secret_name,
                            "defaultMode": 0o400,
                            "items": [
                                {"key": "id_ed25519", "path": "id_ed25519"},
                                {"key": "known_hosts", "path": "known_hosts"},
                            ],
                        },
                    },
                ],
                "initContainers": [
                    {
                        "name": "k8si-restore",
                        "image": k8si_image,
                        "securityContext": {"runAsUser": 0, "runAsGroup": 0},
                        "env": [
                            {"name": "MODE", "value": "restore"},
                            {"name": "RESTORE_SENTINELS", "value": _SENTINEL},
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
                            {
                                "name": "RESTIC_SFTP_COMMAND",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": restic_secret_name,
                                        "key": "RESTIC_SFTP_COMMAND",
                                    },
                                },
                            },
                        ],
                        "volumeMounts": [
                            {"name": "data", "mountPath": "/data"},
                            {"name": "restic-ssh", "mountPath": "/restic-ssh", "readOnly": True},
                        ],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "64Mi"},
                            "limits": {"cpu": "200m", "memory": "256Mi"},
                        },
                    }
                ],
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

    wait_pod_phase(ns, verifier_name, "Running", timeout=300)
    wait_pod_condition(ns, verifier_name, "Ready", timeout=180)
    log.info("Postgres restore verifier pod running and ready")

    proc = _psql_exec(
        ns,
        verifier_name,
        f"SELECT v FROM items WHERE v='{KNOWN_VALUE}';",
    )
    log.info("Verification stdout: %r", proc.stdout)
    assert KNOWN_VALUE in proc.stdout, f"KNOWN_VALUE not found in query result: {proc.stdout!r}"
