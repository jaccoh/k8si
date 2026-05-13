"""Generate init container + sidecar YAML for a deployment manifest."""

import argparse
import sys


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = subparsers.add_parser("generate", help="Generate k8si YAML snippet for a deployment")
    p.add_argument("--app", required=True, help="App name (used for container names)")
    p.add_argument("--pvc", required=True, help="PVC claim name to mount at /data")
    p.add_argument("--secret", required=True, help="Secret name with RESTIC_* keys + SSH key")
    p.add_argument("--sentinel", required=True, help="Sentinel file path relative to PVC root")
    p.add_argument("--schedule", required=True, help="Backup cron schedule, e.g. '0 2 * * *'")
    p.add_argument("--image", default="ghcr.io/jaccoh/k8si:latest", help="k8si image")
    p.add_argument("--retention-daily", type=int, default=7, metavar="N")
    p.add_argument("--retention-weekly", type=int, default=4, metavar="N")
    p.add_argument("--retention-monthly", type=int, default=3, metavar="N")
    p.add_argument("--tags", default="", help="Comma-separated backup tags, e.g. 'app=sonarr'")
    p.add_argument("--no-sidecar", action="store_true", help="Omit the backup sidecar")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    print(_fix_ssh_perms_container(args.secret))
    print(_init_container(args, tags))

    if not args.no_sidecar:
        print(_sidecar(args, tags))

    print(_volume_hint(args))


def _fix_ssh_perms_container(secret: str) -> str:
    return (
        "# ── initContainers (fix-ssh-perms must come before k8si-restore): ────────────\n"
        f"      - name: fix-ssh-perms\n"
        f"        image: busybox:1.37.0\n"
        f"        securityContext:\n"
        f"          runAsUser: 0\n"
        f"        command:\n"
        f"          - sh\n"
        f"          - -c\n"
        f"          - |\n"
        f"            cp /restic-ssh-secret/id_ed25519 /restic-ssh/id_ed25519\n"
        f"            cp /restic-ssh-secret/known_hosts /restic-ssh/known_hosts\n"
        f"            chmod 400 /restic-ssh/id_ed25519\n"
        f"            chmod 644 /restic-ssh/known_hosts\n"
        f"        volumeMounts:\n"
        f"          - name: restic-ssh-secret\n"
        f"            mountPath: /restic-ssh-secret\n"
        f"            readOnly: true\n"
        f"          - name: restic-ssh\n"
        f"            mountPath: /restic-ssh\n"
    )


def _secret_env(name: str, key: str, secret: str) -> str:
    return (
        f"          - name: {name}\n"
        f"            valueFrom:\n"
        f"              secretKeyRef:\n"
        f"                name: {secret}\n"
        f"                key: {key}"
    )


def _init_container(args: argparse.Namespace, tags: list[str]) -> str:
    tag_env = ""
    if tags:
        tag_env = f"\n          - name: BACKUP_TAGS\n            value: \"{','.join(tags)}\""

    return (
        "# ── initContainers: ───────────────────────────────────────────────────────────\n"
        f"      - name: k8si-restore\n"
        f"        image: {args.image}\n"
        f"        securityContext:\n"
        f"          runAsUser: 0\n"
        f"          runAsGroup: 0\n"
        f"        env:\n"
        f"          - name: MODE\n"
        f"            value: restore\n"
        f"          - name: DATA_PATH\n"
        f"            value: /data\n"
        f"          - name: SENTINEL_FILE\n"
        f"            value: {args.sentinel}"
        f"{tag_env}\n"
        f"{_secret_env('RESTIC_REPOSITORY', 'RESTIC_REPOSITORY', args.secret)}\n"
        f"{_secret_env('RESTIC_PASSWORD', 'RESTIC_PASSWORD', args.secret)}\n"
        f"{_secret_env('RESTIC_SFTP_COMMAND', 'RESTIC_SFTP_COMMAND', args.secret)}\n"
        f"        volumeMounts:\n"
        f"          - name: {args.pvc}\n"
        f"            mountPath: /data\n"
        f"          - name: restic-ssh\n"
        f"            mountPath: /restic-ssh\n"
        f"            readOnly: true\n"
    )


def _sidecar(args: argparse.Namespace, tags: list[str]) -> str:
    tag_env = ""
    if tags:
        tag_env = f"\n          - name: BACKUP_TAGS\n            value: \"{','.join(tags)}\""

    return (
        "# ── initContainers (native sidecar, K8s 1.29+): ─────────────────────────────\n"
        f"      - name: k8si-backup\n"
        f"        image: {args.image}\n"
        f"        restartPolicy: Always\n"
        f"        env:\n"
        f"          - name: MODE\n"
        f"            value: backup\n"
        f"          - name: DATA_PATH\n"
        f"            value: /data\n"
        f"          - name: SENTINEL_FILE\n"
        f"            value: {args.sentinel}\n"
        f"          - name: BACKUP_SCHEDULE\n"
        f"            value: \"{args.schedule}\"\n"
        f"          - name: RETENTION_DAILY\n"
        f"            value: \"{args.retention_daily}\"\n"
        f"          - name: RETENTION_WEEKLY\n"
        f"            value: \"{args.retention_weekly}\"\n"
        f"          - name: RETENTION_MONTHLY\n"
        f"            value: \"{args.retention_monthly}\""
        f"{tag_env}\n"
        f"{_secret_env('RESTIC_REPOSITORY', 'RESTIC_REPOSITORY', args.secret)}\n"
        f"{_secret_env('RESTIC_PASSWORD', 'RESTIC_PASSWORD', args.secret)}\n"
        f"{_secret_env('RESTIC_SFTP_COMMAND', 'RESTIC_SFTP_COMMAND', args.secret)}\n"
        f"        volumeMounts:\n"
        f"          - name: {args.pvc}\n"
        f"            mountPath: /data\n"
        f"          - name: restic-ssh\n"
        f"            mountPath: /restic-ssh\n"
        f"            readOnly: true\n"
        f"        resources:\n"
        f"          requests:\n"
        f"            cpu: 50m\n"
        f"            memory: 64Mi\n"
        f"          limits:\n"
        f"            cpu: 200m\n"
        f"            memory: 256Mi\n"
    )


def _volume_hint(args: argparse.Namespace) -> str:
    return f"""\
# ── volumes (add to existing volumes list): ───────────────────────────────────
#       - name: {args.pvc}
#         persistentVolumeClaim:
#           claimName: {args.pvc}
      - name: restic-ssh-secret
        secret:
          secretName: {args.secret}
          defaultMode: 0400
          items:
            - key: id_ed25519
              path: id_ed25519
            - key: known_hosts
              path: known_hosts
      - name: restic-ssh
        emptyDir: {{}}
"""
