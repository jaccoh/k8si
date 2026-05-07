"""Build the CronJob body and restore patch for a K8siBackup resource."""

import os
import textwrap
from typing import Any

K8SI_IMAGE = os.environ.get("K8SI_IMAGE", "ghcr.io/jaccoh/k8si:latest")


def build_restore_patch(spec: dict[str, Any]) -> str:
    """Return a YAML snippet to paste into spec.initContainers of any Deployment."""
    restic_secret = spec["resticSecret"]
    restore = spec.get("restore", {})
    sentinels = restore.get("sentinels", [])
    required = restore.get("required", False)
    max_age = restore.get("maxAge", "")
    size_min = restore.get("size", {}).get("min", "")
    size_max = restore.get("size", {}).get("max", "")
    tags = restore.get("tags", [])

    env_lines = [
        "  - name: MODE\n    value: restore",
    ]
    if sentinels:
        env_lines.append(f"  - name: RESTORE_SENTINELS\n    value: {','.join(sentinels)}")
    if required:
        env_lines.append("  - name: RESTORE_REQUIRED\n    value: \"true\"")
    if max_age:
        env_lines.append(f"  - name: RESTORE_MAX_AGE\n    value: {max_age}")
    if size_min:
        env_lines.append(f"  - name: RESTORE_SIZE_MIN\n    value: {size_min}")
    if size_max:
        env_lines.append(f"  - name: RESTORE_SIZE_MAX\n    value: {size_max}")
    if tags:
        env_lines.append(f"  - name: RESTORE_TAGS\n    value: {','.join(tags)}")

    for var, key in [
        ("RESTIC_REPOSITORY", "RESTIC_REPOSITORY"),
        ("RESTIC_PASSWORD", "RESTIC_PASSWORD"),
        ("RESTIC_SFTP_COMMAND", "RESTIC_SFTP_COMMAND"),
    ]:
        env_lines.append(
            f"  - name: {var}\n"
            f"    valueFrom:\n"
            f"      secretKeyRef:\n"
            f"        name: {restic_secret}\n"
            f"        key: {key}"
        )

    env_block = "\n".join(f"  {line}" if not line.startswith("  ") else line
                          for line in "\n".join(env_lines).splitlines())

    return textwrap.dedent(f"""\
        # --- paste into spec.initContainers ---
        - name: k8si-restore
          image: {K8SI_IMAGE}
          env:
        {env_block}
          volumeMounts:
          - name: data
            mountPath: /data
          - name: restic-ssh
            mountPath: /restic-ssh
            readOnly: true
        # --- paste into spec.volumes (if not already present) ---
        - name: restic-ssh
          secret:
            secretName: {restic_secret}
            defaultMode: 0o400
            items:
            - key: id_ed25519
              path: id_ed25519
            - key: known_hosts
              path: known_hosts
    """)


def build_cronjob(
    name: str,
    namespace: str,
    uid: str,
    spec: dict[str, Any],
    node_name: str | None = None,
) -> dict[str, Any]:
    restic_secret = spec["resticSecret"]
    retention = spec.get("retention", {})
    restore = spec.get("restore", {})
    tags = restore.get("tags", [])

    env: list[dict[str, Any]] = [
        {"name": "MODE", "value": "job"},
        {"name": "DATA_PATH", "value": "/data"},
        {"name": "RETENTION_DAILY", "value": str(retention.get("daily", 7))},
        {"name": "RETENTION_WEEKLY", "value": str(retention.get("weekly", 4))},
        {"name": "RETENTION_MONTHLY", "value": str(retention.get("monthly", 3))},
    ]
    if spec.get("preBackupHook"):
        env.append({"name": "PRE_BACKUP_HOOK", "value": spec["preBackupHook"]})
    if tags:
        env.append({"name": "BACKUP_TAGS", "value": ",".join(tags)})
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
                            **({"nodeSelector": {"kubernetes.io/hostname": node_name}} if node_name else {}),
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
