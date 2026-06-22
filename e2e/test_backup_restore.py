"""End-to-end: SQLite backup via run_backup(), restore via k8si init container."""

import asyncio
import base64
import logging
import subprocess
import uuid

import kubernetes.client

from e2e.helpers import delete_pvc_with_cleanup, wait_pod_deleted, wait_pod_phase
from k8si.operator.workflow import run_backup

log = logging.getLogger(__name__)

KNOWN_VALUE = f"e2e-{uuid.uuid4().hex[:12]}"


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def test_sqlite_backup_and_restore(ns, repo_pvc, data_pvc, k8si_image):
    v1 = kubernetes.client.CoreV1Api()

    writer_name = "e2e-writer"
    v1.create_namespaced_pod(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": writer_name,
                "namespace": ns,
                "labels": {"app": "e2e-writer"},
            },
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": "hoeve-worker01"},
                "restartPolicy": "Never",
                "volumes": [
                    {"name": "data", "persistentVolumeClaim": {"claimName": data_pvc}},
                ],
                "containers": [
                    {
                        "name": "writer",
                        "image": "python:3.13-slim",
                        "command": [
                            "python3",
                            "-c",
                            (
                                "import sqlite3, time; "
                                f"db = sqlite3.connect('/data/test.db'); "
                                "db.execute('PRAGMA journal_mode=WAL'); "
                                "db.execute('CREATE TABLE IF NOT EXISTS items (v TEXT)'); "
                                f"db.execute('INSERT INTO items VALUES (?)', ({KNOWN_VALUE!r},)); "
                                "db.commit(); db.close(); "
                                "[time.sleep(3600) for _ in iter(int, 1)]"
                            ),
                        ],
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
    wait_pod_phase(ns, writer_name, "Running", timeout=180)
    log.info("Writer pod running, KNOWN_VALUE=%s", KNOWN_VALUE)

    secret_name = "e2e-restic-secret"
    v1.create_namespaced_secret(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": secret_name, "namespace": ns},
            "data": {
                "RESTIC_REPOSITORY": _b64("local:/repo"),
                "RESTIC_PASSWORD": _b64("e2etest"),
            },
        },
    )

    spec = {
        "pvc": data_pvc,
        "resticSecret": secret_name,
        "repositoryPVC": repo_pvc,
        "schedule": "0 0 1 1 *",
        "volumeSnapshotClass": "openebs-lvm-snapclass",
        "database": {
            "type": "sqlite",
            "podSelector": {"app": "e2e-writer"},
            "dbPaths": ["/data/test.db"],
        },
        "restore": {"sentinels": ["test.db"]},
    }

    result = asyncio.run(run_backup("e2e", ns, spec, log))
    assert result["lastBackupResult"] == "success", f"Unexpected result: {result}"
    log.info("Backup succeeded: %s", result)

    v1.delete_namespaced_pod(writer_name, ns)
    wait_pod_deleted(ns, writer_name, timeout=60)

    delete_pvc_with_cleanup(ns, data_pvc)

    v1.create_namespaced_persistent_volume_claim(
        ns,
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": data_pvc, "namespace": ns},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": "openebs-lvm-worker-thin",
                "resources": {"requests": {"storage": "100Mi"}},
            },
        },
    )
    log.info("Created fresh empty PVC %s/%s for restore", ns, data_pvc)

    verifier_name = "e2e-verifier"
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
                    {"name": "data", "persistentVolumeClaim": {"claimName": data_pvc}},
                    {"name": "repo", "persistentVolumeClaim": {"claimName": repo_pvc}},
                ],
                "initContainers": [
                    {
                        "name": "k8si-restore",
                        "image": k8si_image,
                        "env": [
                            {"name": "MODE", "value": "restore"},
                            {"name": "RESTORE_SENTINELS", "value": "test.db"},
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
                            {"name": "repo", "mountPath": "/repo"},
                        ],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "64Mi"},
                            "limits": {"cpu": "200m", "memory": "256Mi"},
                        },
                    }
                ],
                "containers": [
                    {
                        "name": "verifier",
                        "image": "python:3.13-slim",
                        "command": ["sh", "-c", "sleep 86400"],
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
    wait_pod_phase(ns, verifier_name, "Running", timeout=300)
    log.info("Restore verifier pod running")

    verify_script = (
        "import sqlite3; "
        "db = sqlite3.connect('/data/test.db'); "
        "rows = db.execute('SELECT v FROM items').fetchall(); "
        f"assert any(r[0] == {KNOWN_VALUE!r} for r in rows), str(rows); "
        "print('OK')"
    )
    proc = subprocess.run(
        ["kubectl", "exec", verifier_name, "-n", ns, "--", "python3", "-c", verify_script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    log.info("Verification stdout: %r", proc.stdout)
    log.info("Verification stderr: %r", proc.stderr)
    assert proc.returncode == 0, f"Verification failed (rc={proc.returncode}): {proc.stderr}"
    assert "OK" in proc.stdout, f"'OK' not in stdout: {proc.stdout!r}"
