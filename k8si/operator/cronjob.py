"""Build the CronJob body for a K8siBackup resource."""

import os
from typing import Any

K8SI_IMAGE = os.environ.get("K8SI_IMAGE", "ghcr.io/jaccoh/k8si:latest")


def build_cronjob(name: str, namespace: str, uid: str, spec: dict[str, Any]) -> dict[str, Any]:
    restic_secret = spec["resticSecret"]
    retention = spec.get("retention", {})

    env: list[dict[str, Any]] = [
        {"name": "MODE", "value": "job"},
        {"name": "DATA_PATH", "value": "/data"},
        {"name": "RETENTION_DAILY", "value": str(retention.get("daily", 7))},
        {"name": "RETENTION_WEEKLY", "value": str(retention.get("weekly", 4))},
        {"name": "RETENTION_MONTHLY", "value": str(retention.get("monthly", 3))},
    ]
    if spec.get("preBackupHook"):
        env.append({"name": "PRE_BACKUP_HOOK", "value": spec["preBackupHook"]})
    for var, key in [
        ("RESTIC_REPOSITORY", "RESTIC_REPOSITORY"),
        ("RESTIC_PASSWORD", "RESTIC_PASSWORD"),
        ("RESTIC_SFTP_COMMAND", "RESTIC_SFTP_COMMAND"),
    ]:
        env.append({
            "name": var,
            "valueFrom": {"secretKeyRef": {"name": restic_secret, "key": key}},
        })

    resources = spec.get("resources", {
        "requests": {"cpu": "50m", "memory": "64Mi"},
        "limits": {"cpu": "200m", "memory": "256Mi"},
    })

    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {
            "name": f"k8si-{name}",
            "namespace": namespace,
            "ownerReferences": [{
                "apiVersion": "k8si.io/v1",
                "kind": "K8siBackup",
                "name": name,
                "uid": uid,
                "blockOwnerDeletion": True,
                "controller": True,
            }],
        },
        "spec": {
            "schedule": spec["schedule"],
            "concurrencyPolicy": "Forbid",
            "successfulJobsHistoryLimit": 3,
            "failedJobsHistoryLimit": 3,
            "jobTemplate": {
                "spec": {
                    "backoffLimit": 0,
                    "template": {
                        "spec": {
                            "restartPolicy": "Never",
                            "volumes": [
                                {
                                    "name": "data",
                                    "persistentVolumeClaim": {"claimName": spec["pvc"]},
                                },
                                {
                                    "name": "restic-ssh",
                                    "secret": {
                                        "secretName": restic_secret,
                                        "defaultMode": 0o400,
                                        "items": [
                                            {"key": "id_ed25519", "path": "id_ed25519"},
                                            {"key": "known_hosts", "path": "known_hosts"},
                                        ],
                                    },
                                },
                            ],
                            "containers": [{
                                "name": "k8si",
                                "image": K8SI_IMAGE,
                                "env": env,
                                "volumeMounts": [
                                    {"name": "data", "mountPath": "/data"},
                                    {"name": "restic-ssh", "mountPath": "/restic-ssh", "readOnly": True},
                                ],
                                "resources": resources,
                            }],
                        },
                    },
                },
            },
        },
    }
