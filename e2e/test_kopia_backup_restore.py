"""End-to-end: kopia backend — backup via run_backup(), restore via init container.

Closes the QA gap from docs/v0.9.0-goals.md: kopia previously only ran against a
mocked CLI, so a real kopia output-format change (snapshot list JSON, "Created
snapshot ... and ID ..." in _parse_artifact, `ls -r` line format in
check_sentinels, `snapshot restore <id> /`) would ship undetected. This test
exercises the real kopia binary in the k8si image against a filesystem repo on a
PVC, starting from an EMPTY repository so the auto-init path
(RepositoryNotInitializedError → init → retry) runs for real.
"""

import asyncio
import base64
import logging
import subprocess
import time
import uuid

import kubernetes.client
import pytest

from e2e.conftest import NODE_NAME, SNAPSHOT_CLASS, STORAGE_CLASS
from e2e.helpers import delete_pvc_with_cleanup, wait_pod_deleted, wait_pod_phase
from k8si.operator import workflow
from k8si.operator.workflow import run_backup

log = logging.getLogger(__name__)

KNOWN_VALUE = f"kopia-e2e-{uuid.uuid4().hex[:12]}"


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


@pytest.fixture
def kopia_backend_type():
    """Flip the workflow module global to kopia for the duration of one test.

    run_backup() reads workflow.BACKEND_TYPE at call time (secret resolution,
    job env, artifact parsing), but the module captured the operator env at
    import — the e2e session imports it with restic as default, so the switch
    must happen per-test and be restored afterwards.
    """
    previous = workflow.BACKEND_TYPE
    workflow.BACKEND_TYPE = "kopia"
    try:
        yield "kopia"
    finally:
        workflow.BACKEND_TYPE = previous


def test_kopia_backup_and_restore(ns, repo_pvc, data_pvc, k8si_image, kopia_backend_type):
    v1 = kubernetes.client.CoreV1Api()

    writer_name = "kopia-e2e-writer"
    v1.create_namespaced_pod(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": writer_name,
                "namespace": ns,
                "labels": {"app": "kopia-e2e-writer"},
            },
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": NODE_NAME},
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
                                "from pathlib import Path; "
                                "import time; "
                                f"Path('/data/payload.txt').write_text({KNOWN_VALUE!r}); "
                                "Path('/data/sentinel.txt').write_text('ok'); "
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
    time.sleep(3)
    log.info("Writer pod running, KNOWN_VALUE=%s", KNOWN_VALUE)

    secret_name = "e2e-kopia-secret"
    v1.create_namespaced_secret(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": secret_name, "namespace": ns},
            "data": {
                # kopia filesystem repo on the repo PVC; empty dir exercises auto-init
                "RESTIC_REPOSITORY": _b64("local:/repo"),
                "RESTIC_PASSWORD": _b64("e2etest"),
            },
        },
    )

    spec = {
        "pvc": data_pvc,
        "kopiaSecret": secret_name,
        "repositoryPVC": repo_pvc,
        "schedule": "0 0 1 1 *",
        "volumeSnapshotClass": SNAPSHOT_CLASS,
        "tags": ["app=kopia-e2e"],
        "restore": {"sentinels": ["sentinel.txt"]},
    }

    result = asyncio.run(run_backup("e2e-kopia", ns, spec, log, run_name="e2e-kopia-run"))
    assert result["lastBackupResult"] == "success", f"Unexpected result: {result}"
    assert result["backendType"] == "kopia", f"Unexpected backendType: {result}"
    # snapshotId/sizeBytes prove _parse_artifact matched the real kopia CLI output
    assert result["snapshotId"], f"No snapshot ID parsed from job logs: {result}"
    assert result["sizeBytes"] is not None, f"No size parsed from job logs: {result}"
    log.info("Kopia backup succeeded: %s", result)

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
                "storageClassName": STORAGE_CLASS,
                "resources": {"requests": {"storage": "128Mi"}},
            },
        },
    )
    log.info("Created fresh empty PVC %s/%s for restore", ns, data_pvc)

    verifier_name = "kopia-e2e-verifier"
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
                    {"name": "data", "persistentVolumeClaim": {"claimName": data_pvc}},
                    {"name": "repo", "persistentVolumeClaim": {"claimName": repo_pvc}},
                ],
                "initContainers": [
                    {
                        "name": "k8si-restore",
                        "image": k8si_image,
                        "env": [
                            {"name": "MODE", "value": "restore"},
                            {"name": "BACKEND_TYPE", "value": "kopia"},
                            {"name": "RESTORE_SENTINELS", "value": "sentinel.txt"},
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
    # wait_pod_phase fast-fails with init-container logs if the restore fails loud
    wait_pod_phase(ns, verifier_name, "Running", timeout=300)
    log.info("Restore verifier pod running")

    proc = subprocess.run(
        ["kubectl", "exec", verifier_name, "-n", ns, "--", "cat", "/data/payload.txt"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    log.info("Verification stdout: %r", proc.stdout)
    log.info("Verification stderr: %r", proc.stderr)
    assert proc.returncode == 0, f"Verification failed (rc={proc.returncode}): {proc.stderr}"
    assert KNOWN_VALUE in proc.stdout, f"Payload not restored: {proc.stdout!r}"
