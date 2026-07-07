# k8si

**Backup is a solved problem. Restore isn't.**

Every cluster has a backup solution. Few have a restore solution. When a PVC disappears, a namespace gets nuked, or a migration goes wrong — someone gets paged. Someone opens a runbook. Someone runs commands under pressure at 2am hoping they got it right.

k8si makes restore automatic. Every pod gets an init container that asks one question on startup: *is my data here?* If yes, it exits in milliseconds. If no — fresh PVC, deleted namespace, botched migration — it finds the latest snapshot and restores before the app ever sees an empty volume. No runbook. No intervention. The pod comes up with its data intact.

GitOps rebuilt your cluster. k8si extends that to your data.

Backups are declared as a `K8siBackup` resource. On schedule the operator creates a `K8siBackupRun` resource; the run reconciler executes the pipeline: optional DB quiescing → VolumeSnapshot → ephemeral PVC clone → restic backup Job → cleanup. In `snapshot` mode your live PVC is never touched during backup. In `direct` mode the backup job mounts the live PVC read-only. `kubectl get k8sibackups` gives a live view of backup health across all apps.

Three components:

| Component | Mode | Description |
|-----------|------|-------------|
| **Restore init container** | `MODE=restore` | Checks sentinel files on the PVC on every pod start; restores from restic if data is missing or incomplete. Fails loud rather than letting the app start on empty or corrupt state. |
| **Operator** | Kopf + `K8siBackup` + `K8siBackupRun` CRDs | Scheduled timer creates a `K8siBackupRun`; the run reconciler drives the pipeline (quiesce → snapshot → clone → Job → cleanup). Reports `lastBackupResult`, `lastRunRef`, and rolling history on the parent CRD. |
| **k8si-ui** | FastAPI dashboard | Web dashboard showing all backups across namespaces — status, schedule, last/next backup, 7-run sparkline, live log drawer, pause/resume, and manual trigger. Exposed via NodePort `:30080`. |

Backend is pluggable — restic over SFTP (Hetzner Storagebox) ships by default. Image: `ghcr.io/jaccoh/k8si` for `linux/amd64` and `linux/arm64`.

---

## Quick start (operator mode)

### 1. Install the CRD, operator, and (optionally) the web UI

```bash
kubectl apply -f https://raw.githubusercontent.com/jaccoh/k8si/main/deploy/crd.yaml
kubectl apply -f https://raw.githubusercontent.com/jaccoh/k8si/main/deploy/crd_run.yaml
kubectl apply -f https://raw.githubusercontent.com/jaccoh/k8si/main/deploy/rbac.yaml
kubectl apply -f https://raw.githubusercontent.com/jaccoh/k8si/main/deploy/operator.yaml

# Optional: read-only status dashboard on NodePort :30080
kubectl apply -f https://raw.githubusercontent.com/jaccoh/k8si/main/deploy/ui.yaml
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
  resticSecret: restic-sonarr-config
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

### Manual trigger

Trigger a backup outside the schedule by patching `status.triggeredAt`:

```bash
kubectl patch k8sibackup sonarr-config -n downloads \
  --type=merge \
  --subresource=status \
  -p '{"status": {"triggeredAt": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}}'
```

The operator picks it up within 60 seconds and runs the backup immediately, bypassing both the schedule check and the backup window. `paused: true` still blocks it.

The dashboard "Backup now" button does the same thing via `POST /api/backups/{ns}/{name}/trigger`.

---

## Architecture

```
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ Cluster                                                                  │
  │                                                                          │
  │  ┌──────────────────┐  timer (60s)  ┌──────────────────────────────┐    │
  │  │ K8siBackup CRD   │──────────────►│ k8si operator (Kopf)         │    │
  │  │                  │◄──────────────│                              │    │
  │  │ spec.schedule    │ status update │ creates K8siBackupRun        │    │
  │  │ spec.pvc         │               └──────────────────────────────┘    │
  │  │ spec.backupMode  │                              │                    │
  │  │ spec.restore     │                   on.create  │                    │
  │  └──────────────────┘                              ▼                    │
  │                                   ┌──────────────────────────────────┐  │
  │                                   │ K8siBackupRun CRD                │  │
  │                                   │                                  │  │
  │                                   │ 1. quiesce DB (optional)         │  │
  │                                   │ 2. VolumeSnapshot                │  │
  │                                   │ 3. clone → ephemeral PVC         │  │
  │                                   │ 4. restic backup Job             │  │
  │                                   │ 5. cleanup                       │  │
  │                                   │                                  │  │
  │                                   │ status.phase: Pending→Succeeded  │  │
  │                                   └──────────────────────────────────┘  │
  │                                                    │                    │
  │                                           restic over SFTP              │
  │                                                    │                    │
  │                                                    ▼                    │
  │                                          Hetzner Storagebox             │
  │                                                                          │
  │  ┌──────────────────────────────────────────────────────────────────┐   │
  │  │ Pod (your app)                                                   │   │
  │  │                                                                  │   │
  │  │  initContainers:                                                 │   │
  │  │    k8si-restore                                                  │   │
  │  │      checks sentinels → restores from restic if missing          │   │
  │  │                                                                  │   │
  │  │  containers:                                                     │   │
  │  │    app + /data PVC                                               │   │
  │  └──────────────────────────────────────────────────────────────────┘   │
  │                                                                          │
  └──────────────────────────────────────────────────────────────────────────┘
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
  resticSecret: <secret-name>       # Secret with RESTIC_* keys + SSH key
  schedule: "0 2 * * *"            # Cron schedule (UTC)
  backupMode: snapshot              # snapshot (default) or direct
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

  # Optional: pause all scheduled backups (manual trigger still works)
  paused: false

  # Optional: restrict backups to a time window (UTC)
  backupWindow:
    start: "02:00"
    end:   "06:00"

  # Optional: max failed retries per calendar day before skipping (default: 3)
  maxRetriesPerDay: 3

  # Optional: backup job timeout in seconds (default: 3600 = 1 hour)
  # Also sets activeDeadlineSeconds on the Job itself so it self-terminates
  jobTimeout: 7200

  # Optional: webhook notifications
  notifyOnSuccess: "https://hooks.example.com/ok"
  notifyOnFailure: "https://hooks.example.com/err"
```

**K8siBackup status fields** (set by the operator, read by `kubectl get k8sibackups`):

| Field | Description |
|-------|-------------|
| `lastBackupResult` | `pending` / `running` / `success` / `failed` |
| `lastBackupTime` | ISO-8601 timestamp of the last completed backup |
| `lastBackupDuration` | Duration of last backup in seconds |
| `nextBackupTime` | ISO-8601 timestamp of the next scheduled backup |
| `message` | Last error message (empty on success) |
| `lastRunRef` | Name of the most recently created `K8siBackupRun` |
| `lastSuccessfulRunRef` | Name of the most recently succeeded `K8siBackupRun` |
| `recentBackups` | Rolling list of the last 30 results — `[{time, result}, …]` |
| `triggeredAt` | Set to trigger a manual backup; cleared when the backup runs |
| `restorePatch` | YAML snippet to paste into your pod spec for the restore init container |
| `lastRestoreResult` | `success` / `failed` — result of the last restore (written by init container) |
| `lastRestoreTime` | ISO-8601 timestamp of the last restore |
| `lastRestoreSnapshotId` | Snapshot ID used for the last restore |
| `lastRestoreMessage` | Error message if restore failed |

---

## K8siBackupRun CRD reference

Runs are created automatically by the operator timer. You can also create them manually to trigger a backup with a specific mode.

```yaml
apiVersion: k8si.io/v1
kind: K8siBackupRun
metadata:
  name: sonarr-config-20260707020000
  namespace: downloads
  labels:
    k8si.io/backup: sonarr-config
spec:
  backupRef: sonarr-config       # Parent K8siBackup name
  triggeredBy: manual            # manual | schedule | backfill
  triggeredAt: "2026-07-07T02:00:00Z"
  mode: snapshot                 # snapshot | direct — overrides parent backupMode
```

**K8siBackupRun status fields**:

| Field | Description |
|-------|-------------|
| `phase` | `Pending` → `Running` → `Succeeded` / `Failed` |
| `startTime` | ISO-8601 timestamp when the run started |
| `completionTime` | ISO-8601 timestamp when the run finished |
| `message` | Error message on failure |
| `snapshotId` | Restic snapshot ID written after successful backup |
| `sizeBytes` | Snapshot size in bytes |
| `backendType` | `restic` or `kopia` |
| `log` | Append-only phase log — `[{time, phase, message}, …]` |

```bash
# List all runs for a backup
kubectl get k8sibackupruns -n downloads -l k8si.io/backup=sonarr-config

# Watch a run in progress
kubectl get k8sibackupruns sonarr-config-20260707020000 -n downloads -w
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

The `BackupBackend` protocol (`k8si.backend`) defines the interface. Two implementations ship:

| Backend | Module | Selected when |
|---------|--------|---------------|
| **restic** (default) | `k8si/backends/restic.py` | `BACKEND_TYPE` unset or `restic` |
| **kopia** | `k8si/backends/kopia.py` | `BACKEND_TYPE=kopia` |

Both speak the same protocol — swap them in `cli.py` or add your own:

```python
from k8si.backend import BackupBackend, BackupError, NoSnapshotsError

class MyBackend:
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

**VolumeSnapshot conflicts.** If another system (e.g. VolSync) is simultaneously snapshotting the same PVC, k8si waits up to 30 minutes (polling every 60s) for the conflict to clear before creating its own snapshot. A warning is logged on first detection. If the conflict never clears, the backup is skipped and retried on the next scheduled run.

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

CI runs on every push to `main` via GitHub Actions: lint (`ruff` + `mypy`) → unit tests → multi-arch image build (linux/amd64 + linux/arm64) pushed to `ghcr.io/jaccoh/k8si:{sha}` and `ghcr.io/jaccoh/k8si-ui:{sha}` → GitHub release tagged `v{version}` from `pyproject.toml`. The release step is idempotent and skips if the tag already exists. e2e tests require a real cluster and run separately on private infrastructure.
