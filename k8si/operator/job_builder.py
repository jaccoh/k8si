"""Backup Job body construction — pure spec → Kubernetes Job dict.

Split out of workflow.py: run_backup is orchestration, this module only
decides what the backup Job looks like (env, volumes, node pinning, timeouts).
"""

from typing import Any

from .cronjob import K8SI_IMAGE, _restic_env_vars

_BACKUP_JOB_TIMEOUT = 3600


def _resolve_backup_secret(spec: dict[str, Any], backend_type: str) -> str:
    """Return the secret name to use for the backup job.

    kopia uses spec.kopiaSecret (falls back to resticSecret for shared SFTP secrets).
    restic always uses spec.resticSecret.
    """
    if backend_type == "kopia":
        return str(spec.get("kopiaSecret") or spec.get("resticSecret") or "")
    return str(spec.get("resticSecret") or "")


def _build_backup_job(
    job_name: str,
    namespace: str,
    pvc_name: str,
    restic_secret: str,
    spec: dict[str, Any],
    tags: list[str],
    retention: dict[str, int],
    node: str | None = None,
    repo_pvc: str | None = None,
    job_timeout: int = _BACKUP_JOB_TIMEOUT,
    backend_type: str = "restic",
) -> dict[str, Any]:
    """Build the backup Job body for the given backend.

    *backend_type* is the effective per-backup backend (spec.backendType
    falling back to the operator-wide BACKEND_TYPE — resolved by the caller)
    and decides the container's BACKEND_TYPE env.
    """
    env: list[dict[str, Any]] = [
        {"name": "MODE", "value": "job"},
        {"name": "DATA_PATH", "value": "/data"},
        {"name": "BACKEND_TYPE", "value": backend_type},
        {"name": "RETENTION_DAILY", "value": str(retention.get("daily", 7))},
        {"name": "RETENTION_WEEKLY", "value": str(retention.get("weekly", 4))},
        {"name": "RETENTION_MONTHLY", "value": str(retention.get("monthly", 3))},
    ]
    if tags:
        env.append({"name": "BACKUP_TAGS", "value": ",".join(tags)})
    if spec.get("checkAfterBackup"):
        env.append({"name": "RUN_CHECK", "value": "true"})
    env.extend(_restic_env_vars(restic_secret))

    resources = spec.get(
        "resources",
        {
            "requests": {"cpu": "50m", "memory": "128Mi"},
            "limits": {"cpu": "200m", "memory": "1Gi"},
        },
    )

    volumes: list[dict[str, Any]] = [
        {"name": "data", "persistentVolumeClaim": {"claimName": pvc_name}},
        {
            "name": "restic-ssh",
            "secret": {
                "secretName": restic_secret,
                "optional": True,
                "defaultMode": 0o400,
                "items": [
                    {"key": "id_ed25519", "path": "id_ed25519"},
                    {"key": "known_hosts", "path": "known_hosts"},
                ],
            },
        },
    ]
    volume_mounts: list[dict[str, Any]] = [
        {"name": "data", "mountPath": "/data"},
        {"name": "restic-ssh", "mountPath": "/restic-ssh", "readOnly": True},
    ]
    if repo_pvc:
        volumes.append({"name": "repo", "persistentVolumeClaim": {"claimName": repo_pvc}})
        volume_mounts.append({"name": "repo", "mountPath": "/repo"})

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name, "namespace": namespace},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": job_timeout,
            "ttlSecondsAfterFinished": 600,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    **({"nodeSelector": {"kubernetes.io/hostname": node}} if node else {}),
                    "volumes": volumes,
                    "containers": [
                        {
                            "name": "k8si",
                            "image": K8SI_IMAGE,
                            "env": env,
                            "volumeMounts": volume_mounts,
                            "resources": resources,
                        }
                    ],
                }
            },
        },
    }
