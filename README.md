# k8si

**Backup is a solved problem. Restore isn't.**

Every cluster has a backup solution. Few have a restore solution. When a PVC disappears, a namespace gets nuked, or a migration goes wrong — someone gets paged. Someone opens a runbook. Someone runs commands under pressure at 2am hoping they got it right.

k8si makes restore automatic. Every pod gets an init container that asks one question on startup: *is my data here?* If yes, it exits in milliseconds. If no — fresh PVC, deleted namespace, botched migration — it finds the latest snapshot and restores before the app ever sees an empty volume. No runbook. No intervention. The pod comes up with its data intact.

GitOps rebuilt your cluster. k8si extends that to your data.

Backups are declared as a `K8siBackup` resource. The operator takes a consistent VolumeSnapshot on schedule — with optional DB quiescing for Postgres, MariaDB, and SQLite — clones it to an ephemeral PVC, runs a restic backup Job against the clone, then cleans up. Your live PVC is never touched during backup. `kubectl get k8sibackups` gives a live view of backup health across all apps.

Two components, one image:

| Component | Mode | Description |
|-----------|------|-------------|
| **Restore init container** | `MODE=restore` | Checks sentinel files on the PVC on every pod start; restores from restic if data is missing or incomplete. Fails loud rather than letting the app start on empty or corrupt state. |
| **Operator** | Kopf + `K8siBackup` CRD | Owns the full backup pipeline: scheduled VolumeSnapshot (with optional DB quiescing) → ephemeral PVC clone → restic Job → cleanup. Reports `lastBackupResult` on the CRD. |

Backend is pluggable — restic over SFTP (Hetzner Storagebox) ships by default. Image: `ghcr.io/jaccoh/k8si` for `linux/amd64` and `linux/arm64`.

---

## Quick start (operator mode)

### 1. Install the CRD and operator

```bash
kubectl apply -f https://raw.githubusercontent.com/jaccoh/k8si/main/deploy/crd.yaml
kubectl apply -f https://raw.githubusercontent.com/jaccoh/k8si/main/deploy/rbac.yaml
kubectl apply -f https://raw.githubusercontent.com/jaccoh/k8si/main/deploy/operator.yaml
```

### 2. Create the restic secret

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: restic-sonarr-config
  namespace: downloads
stringData:
  RESTIC_REPOSITORY: "sftp:u12345@u12345.your-storagebox.de:backup/sonarr-config"
  RESTIC_PASSWORD: "your-restic-repo-password"
  RESTIC_SFTP_COMMAND: "ssh -i /restic-ssh/id_ed25519 -p 23 -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/restic-ssh/known_hosts -o HostKeyAlgorithms=ecdsa-sha2-nistp521 u12345@u12345.your-storagebox.de -s sftp"
  id_ed25519: |
    -----BEGIN OPENSSH PRIVATE KEY-----
    ...
    -----END OPENSSH PRIVATE KEY-----
  known_hosts: "u12345.your-storagebox.de ecdsa-sha2-nistp521 AAAA..."
```

### 3. Create a K8siBackup resource

```yaml
apiVersion: k8si.io/v1
kind: K8siBackup
metadata:
  name: sonarr-config
  namespace: downloads
spec:
  pvc: sonarr-config
  secret: restic-sonarr-config
  schedule: "0 2 * * *"
  restore:
    sentinels: ["config.xml"]
    required: false
  retention:
    daily: 7
    weekly: 4
    monthly: 3
  tags: ["app=sonarr"]
```

### 4. Check status

```bash
kubectl get k8sibackups -A
# NAMESPACE   NAME            LAST-BACKUP            RESULT    NEXT-BACKUP
# downloads   sonarr-config   2026-05-08T02:00:00Z   success   2026-05-09T02:00:00Z
```

### 5. Add the restore init container to your Deployment

The operator generates the init container YAML and writes it to `.status.restorePatch`. Fetch and paste it:

```bash
kubectl get k8sibackup sonarr-config -n downloads \
  -o jsonpath='{.status.restorePatch}'
```

Or generate it offline with `k8si generate`:

```bash
docker run --rm ghcr.io/jaccoh/k8si:latest generate \
  --app sonarr \
  --pvc sonarr-config \
  --secret restic-sonarr-config \
  --sentinel config.xml \
  --schedule "0 2 * * *"
```

---

## Architecture

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │ Cluster                                                             │
  │                                                                     │
  │  ┌──────────────────┐   reconciles    ┌──────────────────────────┐  │
  │  │ K8siBackup CRD   │────────────────►│ k8si operator (Kopf)    │  │
  │  │                  │◄────────────────│                          │  │
  │  │ spec.schedule    │  status updates │ 1. quiesce DB (optional) │  │
  │  │ spec.pvc         │                 │ 2. VolumeSnapshot        │  │
  │  │ spec.database    │                 │ 3. clone → ephemeral PVC │  │
  │  │ spec.restore     │                 │ 4. restic backup Job     │  │
  │  └──────────────────┘                 │ 5. cleanup               │  │
  │                                       └──────────────────────────┘  │
  │                                                 │                   │
  │                                        restic over SFTP             │
  │                                                 │                   │
  │                                                 ▼                   │
  │                                       Hetzner Storagebox            │
  │                                                                     │
  │  ┌──────────────────────────────────────────────────────────────┐   │
  │  │ Pod (your app)                                               │   │
  │  │                                                              │   │
  │  │  initContainers:                                             │   │
  │  │    k8si-restore                                              │   │
  │  │      checks sentinels → restores from restic if missing      │   │
  │  │                                                              │   │
  │  │  containers:                                                 │   │
  │  │    app + /data PVC                                           │   │
  │  └──────────────────────────────────────────────────────────────┘   │
  │                                                                     │
  └─────────────────────────────────────────────────────────────────────┘
```

---

## Restore behavior

The init container (`MODE=restore`) runs once per pod start and makes exactly one decision:

| Condition | Action |
|-----------|--------|
| `.k8si-no-restore` file on the PVC | Skip (emergency override, no git commit needed) |
| All sentinels present on disk | Skip — data is healthy |
| Marker present, sentinels missing | **Fail loud** — post-restore corruption |
| No sentinels configured, marker present | Skip — already initialized |
| No snapshots found, `restore.required: false` | Skip — first deploy, start fresh |
| No snapshots found, `restore.required: true` | **Fail loud** |
| Snapshot fails sentinel quality gate | Skip |
| Snapshot outside age/size bounds | Skip |
| Restore succeeds, sentinels appear | Write `.k8si-restore-complete` marker, continue |
| Restore fails | **Fail loud** — pod stays in Init:Error |

**Sentinel files** are written by the app, not by k8si. They represent "app fully initialized" — e.g. Sonarr writes `config.xml`, Nextcloud writes `config/config.php`. k8si checks that the sentinel exists both in the snapshot (before restoring) and on disk (after restoring).

**Auto-init**: the backup cycle automatically runs `restic init` on first use. No manual init job needed.

---

## K8siBackup CRD reference

```yaml
apiVersion: k8si.io/v1
kind: K8siBackup
metadata:
  name: <app>
  namespace: <namespace>
spec:
  pvc: <pvc-claim-name>             # PVC to back up, mounted at /data
  secret: <secret-name>             # Secret with RESTIC_* keys + SSH key
  schedule: "0 2 * * *"            # Cron schedule (UTC)
  tags: ["app=sonarr"]             # Optional restic tags

  restore:
    sentinels: ["config.xml"]      # Files that prove data integrity
    required: false                # true = fail loud if no snapshot found
    maxAge: "7d"                   # Skip restore if snapshot older than this
    size:
      min: "1Mi"                   # Skip if snapshot smaller than this
      max: "50Gi"                  # Skip if snapshot larger than this
    tags: ["app=sonarr"]          # Filter snapshots by tag on restore
    snapshot: ""                   # Pin to a specific snapshot ID (override)

  retention:
    daily: 7
    weekly: 4
    monthly: 3
```

---

## Secret format

One Secret per app. All five keys are required.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: restic-<app>
  namespace: <namespace>
stringData:
  RESTIC_REPOSITORY: "sftp:u12345@u12345.your-storagebox.de:backup/<app>"
  RESTIC_PASSWORD: "<password>"
  RESTIC_SFTP_COMMAND: "ssh -i /restic-ssh/id_ed25519 -p 23 -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/restic-ssh/known_hosts -o HostKeyAlgorithms=ecdsa-sha2-nistp521 u12345@u12345.your-storagebox.de -s sftp"
  id_ed25519: |
    -----BEGIN OPENSSH PRIVATE KEY-----
    ...
    -----END OPENSSH PRIVATE KEY-----
  known_hosts: "u12345.your-storagebox.de ecdsa-sha2-nistp521 AAAA..."
```

The `id_ed25519` and `known_hosts` keys are projected to `/restic-ssh/` as files with mode `0400`.

---

## Environment variable reference

### Both modes

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODE` | yes | — | `restore`, `backup`, or `job` |
| `DATA_PATH` | no | `/data` | PVC mount path |
| `RESTIC_REPOSITORY` | yes | — | Full restic repository URL |
| `RESTIC_PASSWORD` | yes* | — | Restic encryption password |
| `RESTIC_PASSWORD_FILE` | yes* | — | Path to password file |

*Either `RESTIC_PASSWORD` or `RESTIC_PASSWORD_FILE` must be set.

### Restore mode (`MODE=restore`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RESTORE_SENTINELS` | no | — | Comma-separated sentinel file paths |
| `RESTORE_REQUIRED` | no | `false` | Fail if no snapshot found |
| `RESTORE_MAX_AGE` | no | — | Max snapshot age (e.g. `7d`, `168h`) |
| `RESTORE_SIZE_MIN` | no | — | Min snapshot size (e.g. `1Mi`, `500Ki`) |
| `RESTORE_SIZE_MAX` | no | — | Max snapshot size (e.g. `50Gi`) |
| `RESTORE_TAGS` | no | — | Comma-separated tags to filter snapshots |
| `RESTORE_SNAPSHOT` | no | — | Pin to a specific snapshot ID |

### Backup mode (`MODE=backup` or `MODE=job`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BACKUP_SCHEDULE` | yes (sidecar) | — | Cron expression (e.g. `0 2 * * *`) |
| `RETENTION_DAILY` | no | `7` | Daily snapshots to keep |
| `RETENTION_WEEKLY` | no | `4` | Weekly snapshots to keep |
| `RETENTION_MONTHLY` | no | `3` | Monthly snapshots to keep |
| `PRE_SNAPSHOT_HOOK` | no | — | Absolute path to script run before the VolumeSnapshot is taken |
| `BACKUP_TAGS` | no | — | Comma-separated tags (e.g. `app=sonarr`) |

---

## Backend plugins

The `BackupBackend` protocol (`k8si.backend`) defines the interface. `k8si/backends/restic.py` is the default implementation using the [sh](https://sh.readthedocs.io/) library. To add a kopia backend, implement the same protocol in `k8si/backends/kopia.py` and pass it to `cli.py`.

```python
from k8si.backend import BackupBackend, BackupError, NoSnapshotsError

class KopiaBackend:
    def init(self) -> None: ...
    def snapshots(self, tags=None) -> list[dict]: ...
    def ls(self, snapshot_id: str) -> list[str]: ...
    def snapshot_size(self, snapshot_id: str) -> int: ...
    def restore(self, snapshot_id: str = "latest") -> None: ...
    def backup(self, source, tags=None) -> None: ...
    def forget(self, daily, weekly, monthly, prune=True) -> None: ...
```

---

## Limitations

**No alerting.** Check `DATA_PATH/.k8si-last-backup` for the timestamp of the last successful backup. Wire this into your monitoring (e.g. `file_mtime_seconds` via node-exporter).

**No stale lock cleanup.** If a backup Job is interrupted, restic may leave a lock. Fix manually:

```bash
kubectl exec -n <namespace> <backup-job-pod> -- restic unlock
```

**Runs as root.** Restic needs root to preserve file ownership on restore. Non-root restore produces wrong permissions silently.

**K8s 1.29+ required** for native sidecar (`restartPolicy: Always` in `initContainers`).

---

## Development

```bash
uv sync
uv run pytest tests/ -v

# Build image
docker build -t k8si:dev .
```

CI runs on every push: tests on all branches, multi-arch image push to `ghcr.io/jaccoh/k8si` on `main`. Releases are tagged `v*` and pushed with `v1.2.3 / v1.2 / v1 / latest` tags.
