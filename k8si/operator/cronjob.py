"""Build the restore init-container patch for a K8siBackup resource."""

import os
from typing import Any

import yaml

K8SI_IMAGE = os.environ.get("K8SI_IMAGE", "ghcr.io/jaccoh/k8si:latest")


def build_restore_patch(spec: dict[str, Any]) -> str:
    """Return a YAML snippet to paste into spec.initContainers and spec.volumes."""
    restic_secret = spec["resticSecret"]
    restore = spec.get("restore", {})
    sentinels = restore.get("sentinels", [])
    required = restore.get("required", False)
    max_age = restore.get("maxAge", "")
    size_min = restore.get("size", {}).get("min", "")
    size_max = restore.get("size", {}).get("max", "")
    tags = restore.get("tags", spec.get("tags", []))

    env: list[dict[str, Any]] = [{"name": "MODE", "value": "restore"}]
    if sentinels:
        env.append({"name": "RESTORE_SENTINELS", "value": ",".join(sentinels)})
    if required:
        env.append({"name": "RESTORE_REQUIRED", "value": "true"})
    if max_age:
        env.append({"name": "RESTORE_MAX_AGE", "value": str(max_age)})
    if size_min:
        env.append({"name": "RESTORE_SIZE_MIN", "value": str(size_min)})
    if size_max:
        env.append({"name": "RESTORE_SIZE_MAX", "value": str(size_max)})
    if tags:
        env.append({"name": "RESTORE_TAGS", "value": ",".join(tags)})
    for var, key in [
        ("RESTIC_REPOSITORY", "RESTIC_REPOSITORY"),
        ("RESTIC_PASSWORD", "RESTIC_PASSWORD"),
        ("RESTIC_SFTP_COMMAND", "RESTIC_SFTP_COMMAND"),
    ]:
        env.append(
            {
                "name": var,
                "valueFrom": {"secretKeyRef": {"name": restic_secret, "key": key}},
            }
        )

    fix_ssh_perms: dict[str, Any] = {
        "name": "fix-ssh-perms",
        "image": "busybox:1.37.0",
        "securityContext": {"runAsUser": 0},
        "command": [
            "sh",
            "-c",
            (
                "cp /restic-ssh-secret/id_ed25519 /restic-ssh/id_ed25519\n"
                "cp /restic-ssh-secret/known_hosts /restic-ssh/known_hosts\n"
                "chmod 400 /restic-ssh/id_ed25519\n"
                "chmod 644 /restic-ssh/known_hosts\n"
            ),
        ],
        "volumeMounts": [
            {"name": "restic-ssh-secret", "mountPath": "/restic-ssh-secret", "readOnly": True},
            {"name": "restic-ssh", "mountPath": "/restic-ssh"},
        ],
    }

    patch: list[dict[str, Any]] = [
        fix_ssh_perms,
        {
            "name": "k8si-restore",
            "image": K8SI_IMAGE,
            "securityContext": {"runAsUser": 0, "runAsGroup": 0},
            "env": env,
            "volumeMounts": [
                {"name": "data", "mountPath": "/data"},
                {"name": "restic-ssh", "mountPath": "/restic-ssh", "readOnly": True},
            ],
        },
        {
            "name": "restic-ssh-secret",
            "secret": {
                "secretName": restic_secret,
                "defaultMode": 0o400,
                "items": [
                    {"key": "id_ed25519", "path": "id_ed25519"},
                    {"key": "known_hosts", "path": "known_hosts"},
                ],
            },
        },
        {
            "name": "restic-ssh",
            "emptyDir": {},
        },
    ]
    header = (
        "# Items 0-1: paste into spec.initContainers (fix-ssh-perms must come first)\n"
        "# Items 2-3: paste into spec.volumes (if not already present)\n"
    )
    return header + yaml.dump(patch, default_flow_style=False, sort_keys=False)  # type: ignore[no-any-return]
