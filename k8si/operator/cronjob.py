"""Build the restore init-container patch for a K8siBackup resource."""

import os
from typing import Any

import yaml

K8SI_IMAGE = os.environ.get("K8SI_IMAGE", "ghcr.io/jaccoh/k8si:latest")

# Required keys — pod fails to start if missing.
_REQUIRED_SECRET_KEYS = ["RESTIC_REPOSITORY", "RESTIC_PASSWORD"]
# Optional keys — absent for file:// and REST backends; only needed for SFTP.
_OPTIONAL_SECRET_KEYS = ["RESTIC_SFTP_COMMAND"]


def _restic_env_vars(secret_name: str) -> list[dict[str, Any]]:
    """Return env var entries that pull backup credentials from *secret_name*."""
    env = [
        {"name": k, "valueFrom": {"secretKeyRef": {"name": secret_name, "key": k}}}
        for k in _REQUIRED_SECRET_KEYS
    ]
    env += [
        {
            "name": k,
            "valueFrom": {"secretKeyRef": {"name": secret_name, "key": k, "optional": True}},
        }
        for k in _OPTIONAL_SECRET_KEYS
    ]
    return env


def build_restore_patch(spec: dict[str, Any], name: str = "", namespace: str = "") -> str:
    """Return a YAML snippet to paste into spec.initContainers and spec.volumes.

    *name*/*namespace* identify the K8siBackup CR this patch belongs to. When
    *name* is given the patch emits ``K8SI_BACKUP_NAME``/``K8SI_BACKUP_NAMESPACE``
    so the restore init container can report provenance back onto the CR status
    (``lastRestoreResult`` & co. — see ``k8si/restore.py``). Without a name the
    vars are omitted and restore reporting stays off, which is the default.
    """
    # Per-CR backendType wins over the operator-wide BACKEND_TYPE (CRD contract).
    backend_type = str(spec.get("backendType") or "").strip().lower()
    if not backend_type:
        backend_type = os.environ.get("BACKEND_TYPE", "restic").lower().strip()
    # kopia uses kopiaSecret; fall back to resticSecret for shared SFTP configs
    if backend_type == "kopia":
        restic_secret = spec.get("kopiaSecret") or spec.get("resticSecret", "")
    else:
        restic_secret = spec.get("resticSecret", "")
    restore = spec.get("restore", {})
    sentinels = restore.get("sentinels", [])
    required = restore.get("required", False)
    max_age = restore.get("maxAge", "")
    size_min = restore.get("size", {}).get("min", "")
    size_max = restore.get("size", {}).get("max", "")
    tags = restore.get("tags", spec.get("tags", []))

    env: list[dict[str, Any]] = [
        {"name": "MODE", "value": "restore"},
        {"name": "BACKEND_TYPE", "value": backend_type},
    ]
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
    if name:
        env.append({"name": "K8SI_BACKUP_NAME", "value": str(name)})
        env.append({"name": "K8SI_BACKUP_NAMESPACE", "value": str(namespace or "default")})
    env.extend(_restic_env_vars(restic_secret))

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
    dumped: str = yaml.dump(patch, default_flow_style=False, sort_keys=False)
    return header + dumped
