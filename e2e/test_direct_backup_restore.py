"""End-to-end: direct backup of live PVC (non-CSI), restore via k8si init container."""

import asyncio
import base64
import logging
import subprocess
import time
import uuid

import kubernetes.client

from e2e.conftest import NODE_NAME, STORAGE_CLASS
from e2e.helpers import delete_pvc_with_cleanup, wait_pod_deleted, wait_pod_phase
from k8si.operator.workflow import run_backup

log = logging.getLogger(__name__)

KNOWN_VALUE = f"e2e-direct-{uuid.uuid4().hex[:12]}"


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def test_direct_backup_and_restore(ns, repo_pvc, k8si_image):
    v1 = kubernetes.client.CoreV1Api()

    # 1. Create a PVC for our test application
    pvc_name = "e2e-direct-pvc"
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

    # 2. Start a writer pod to populate the SQLite database on the live volume
    writer_name = "e2e-writer-direct"
    v1.create_namespaced_pod(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": writer_name,
                "namespace": ns,
                "labels": {"app": "e2e-writer-direct"},
            },
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": NODE_NAME},
                "restartPolicy": "Never",
                "volumes": [
                    {"name": "data", "persistentVolumeClaim": {"claimName": pvc_name}},
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

    # Give the writer a moment to finish the INSERT and commit, then stop it.
    # LINSTOR / RWO volumes are single-attach: the backup Job cannot mount the PVC
    # while the writer pod holds it. Delete the writer to release the mount.
    time.sleep(5)
    v1.delete_namespaced_pod(writer_name, ns)
    wait_pod_deleted(ns, writer_name, timeout=60)
    log.info("Writer pod deleted; PVC is now free for the backup job")

    # 3. Create the Restic secret pointing to our local rest-server
    secret_name = "e2e-restic-secret-direct"
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

    # 4. Construct K8siBackup spec with backupMode: direct (no database quiesce needed —
    #    writer pod is already stopped, PVC is idle)
    spec = {
        "pvc": pvc_name,
        "resticSecret": secret_name,
        "repositoryPVC": repo_pvc,
        "backupMode": "direct",
        "schedule": "0 0 1 1 *",
        "restore": {"sentinels": ["test.db"]},
    }

    # 5. Run the direct backup pipeline
    body = {
        "apiVersion": "k8si.io/v1",
        "kind": "K8siBackup",
        "metadata": {"name": "e2e-direct", "namespace": ns},
    }
    result = asyncio.run(run_backup("e2e-direct", ns, spec, log, body))
    assert result["lastBackupResult"] == "success", f"Unexpected result: {result}"
    log.info("Direct backup succeeded: %s", result)

    # 6. Delete and recreate the PVC to simulate volume loss
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
                "resources": {"requests": {"storage": "100Mi"}},
            },
        },
    )
    log.info("Created fresh empty PVC %s/%s for restore", ns, pvc_name)

    # 7. Start a verifier pod equipped with k8si-restore init container
    verifier_name = "e2e-verifier-direct"
    v1.create_namespaced_pod(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": verifier_name, "namespace": ns},
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": NODE_NAME},
                "restartPolicy": "Never",
                "volumes": [
                    {"name": "data", "persistentVolumeClaim": {"claimName": pvc_name}},
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

    # 8. Verify the SQLite database is successfully restored and populated with our known value
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
    log.info("E2E Direct Backup & Restore verified successfully!")
