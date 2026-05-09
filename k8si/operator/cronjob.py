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
        env.append({
            "name": var,
            "valueFrom": {"secretKeyRef": {"name": restic_secret, "key": key}},
        })

    patch: list[dict[str, Any]] = [
        {
            "name": "k8si-restore",
            "image": K8SI_IMAGE,
            "env": env,
            "volumeMounts": [
                {"name": "data", "mountPath": "/data"},
                {"name": "restic-ssh", "mountPath": "/restic-ssh", "readOnly": True},
            ],
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
    ]
    header = (
        "# First item: paste into spec.initContainers\n"
        "# Second item: paste into spec.volumes (if not already present)\n"
    )
    return header + yaml.dump(patch, default_flow_style=False, sort_keys=False)
