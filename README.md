# k8si

One image, two containers: an init container that restores your app's PVC from restic before the app starts, and a native sidecar that backs it up on a cron schedule. If the backup never ran yet, the pod starts clean. If the backup exists but restore fails, the pod stays in Init and the app never sees empty data.

Backend is restic over SFTP to Hetzner Storagebox. Image is published to `ghcr.io/jaccoh/k8si` for `linux/amd64` and `linux/arm64`.

---

## Architecture

```
                        ┌─────────────────────────────────────────┐
                        │  Pod                                     │
                        │                                          │
  ┌──────────────────┐  │  ┌─────────────┐    ┌────────────────┐  │
  │ Hetzner          │  │  │ k8si-restore│    │ k8si-backup    │  │
  │ Storagebox       │  │  │ initContainer│   │ initContainer  │  │
  │ (restic over     │◄─┼──│             │    │ restartPolicy: │  │
  │  SFTP)           │  │  │ sentinel?   │    │ Always         │  │
  │                  │  │  │  yes → skip │    │                │  │
  │                  │◄─┼──│  no  → pull │    │ cron loop:     │  │
  │                  │  │  │             │    │  backup + forget│  │
  └──────────────────┘  │  └──────┬──────┘    └───────┬────────┘  │
                        │         │                    │           │
                        │         ▼                    │           │
                        │  ┌─────────────┐  PVC        │           │
                        │  │ App         │◄────────────┘           │
                        │  │ container   │  shared mount           │
                        │  └─────────────┘                         │
                        └─────────────────────────────────────────┘

  Flow on pod start:
  1. k8si-restore checks DATA_PATH/SENTINEL_FILE
     - present  → skip (app already initialized, nothing to do)
     - missing, no snapshots → exit 0 (first deploy, start fresh)
     - missing, snapshot found → restore latest to / → exit 0
     - restore error → exit 1 (pod stays in Init, app never starts)
  2. App container starts, writes sentinel on first init
  3. k8si-backup sidecar runs independently on BACKUP_SCHEDULE cron
```

---

## Quick start

### 1. Create the restic secret

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

Initialize the restic repository once (run this as a Job or locally with restic installed):

```bash
restic -r sftp:u12345@u12345.your-storagebox.de:backup/sonarr-config \
  -o sftp.command="ssh -i /path/to/id_ed25519 -p 23 ..." \
  init
```

### 2. Generate the YAML snippet

```bash
k8si generate \
  --app sonarr \
  --pvc sonarr-config \
  --secret restic-sonarr-config \
  --sentinel config.xml \
  --schedule "0 2 * * *"
```

Or with Docker (no local install needed):

```bash
docker run --rm ghcr.io/jaccoh/k8si:latest generate \
  --app sonarr \
  --pvc sonarr-config \
  --secret restic-sonarr-config \
  --sentinel config.xml \
  --schedule "0 2 * * *"
```

### 3. Paste into your deployment

Add the output to your Deployment/StatefulSet `spec.template.spec`. The generator prints three blocks: the restore init container, the backup sidecar, and the volume stanza. The PVC volume entry is commented out — uncomment it only if you do not already have it in your volumes list.

### 4. Verify

Check restore ran (or was correctly skipped on first deploy):

```bash
kubectl logs <pod-name> -c k8si-restore
```

Check backups are running:

```bash
kubectl logs <pod-name> -c k8si-backup --follow
```

Confirm snapshots exist in restic (run from any pod with the secret mounted, or locally):

```bash
restic -r sftp:u12345@u12345.your-storagebox.de:backup/sonarr-config \
  -o sftp.command="..." \
  snapshots
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
  # Full restic repository URL (SFTP form)
  RESTIC_REPOSITORY: "sftp:u12345@u12345.your-storagebox.de:backup/<app>"

  # Restic encryption password
  RESTIC_PASSWORD: "<password>"

  # Full SSH command passed to restic via -o sftp.command=...
  # Port 23 is Hetzner Storagebox's SFTP port.
  # StrictHostKeyChecking=yes requires known_hosts below.
  RESTIC_SFTP_COMMAND: "ssh -i /restic-ssh/id_ed25519 -p 23 -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/restic-ssh/known_hosts -o HostKeyAlgorithms=ecdsa-sha2-nistp521 u12345@u12345.your-storagebox.de -s sftp"

  # ED25519 private key (no passphrase)
  id_ed25519: |
    -----BEGIN OPENSSH PRIVATE KEY-----
    ...
    -----END OPENSSH PRIVATE KEY-----

  # Known hosts entry for the storagebox (obtain with ssh-keyscan -p 23 u12345.your-storagebox.de)
  known_hosts: "u12345.your-storagebox.de ecdsa-sha2-nistp521 AAAA..."
```

The `id_ed25519` and `known_hosts` keys are projected to `/restic-ssh/` as files with mode `0400`. The `RESTIC_SFTP_COMMAND` path `/restic-ssh/id_ed25519` matches this mount.

---

## `k8si generate` flag reference

All flags are required unless marked optional.

| Flag | Description |
|------|-------------|
| `--app` | App name, used for container names (`k8si-restore`, `k8si-backup`) |
| `--pvc` | PVC claim name, mounted at `/data` in both containers |
| `--secret` | Secret name (must contain all five keys above) |
| `--sentinel` | Sentinel file path relative to PVC root (e.g. `config.xml`, `config/config.php`) |
| `--schedule` | Cron expression for backup schedule (e.g. `"0 2 * * *"`) |
| `--image` | k8si image to use (default: `ghcr.io/jaccoh/k8si:latest`) |
| `--retention-daily` | Keep N daily snapshots (default: `7`) |
| `--retention-weekly` | Keep N weekly snapshots (default: `4`) |
| `--retention-monthly` | Keep N monthly snapshots (default: `3`) |
| `--tags` | Comma-separated backup tags (optional, e.g. `app=sonarr`) |
| `--no-sidecar` | Omit the backup sidecar (restore init container only) |

---

## Restore behavior

The restore init container (`MODE=restore`) runs once per pod start. Behavior depends on the sentinel file at `DATA_PATH/SENTINEL_FILE`.

**Sentinel present** (`config.xml` exists on the PVC):
The app was already initialized. k8si logs a skip message and exits 0 immediately. No restic calls are made.

**Sentinel missing, no snapshots in the repository** (first deploy or empty repo):
`restic restore latest` reports no matching snapshot. k8si catches this, logs "first deploy, starting fresh", and exits 0. The app starts with an empty PVC and writes the sentinel on its own first initialization.

**Sentinel missing, snapshot found**:
k8si runs `restic restore latest --target /`. This restores the snapshot to the filesystem root, preserving the original path structure (e.g. `/data/config.xml`). On success k8si exits 0 and the app starts with its previous state intact.

**Restore fails** (SFTP unreachable, wrong password, corrupt snapshot, etc.):
k8si logs the restic stderr and exits 1. The pod stays in `Init:Error`. The app container never starts. The PVC is not touched. Fix the underlying issue and delete the pod — a new pod will retry.

The sentinel is written by the **app**, not by k8si. It represents "app fully initialized", not just "files present". Examples: Sonarr writes `config.xml`, Nextcloud writes `config/config.php`. Choose a file that only appears after the app has completed its first-run setup.

---

## Backup behavior

The backup sidecar (`MODE=backup`) runs as a Kubernetes native sidecar (`restartPolicy: Always` in `initContainers`). It requires Kubernetes 1.29+. Native sidecars start before app containers and are restarted independently — the app does not restart when the sidecar exits.

**Cron loop**: k8si computes the next scheduled time using `croniter`, sleeps until then, then runs a backup cycle. The schedule follows standard cron syntax. Timezone is UTC.

**Each backup cycle**:
1. Optional pre-backup hook (`PRE_BACKUP_HOOK`) runs first. Hook failures are logged but do not abort the backup — the restic backup proceeds regardless.
2. `restic backup /data [--tag ...]` runs.
3. `restic forget --keep-daily N --keep-weekly N --keep-monthly N --prune` runs.
4. A timestamp is written to `DATA_PATH/.k8si-last-backup` for external monitoring.

**Error handling**: if `restic backup` or `restic forget` fails, the error is logged and the sidecar continues. It will retry at the next scheduled interval. The sidecar never exits voluntarily — it runs until the pod is deleted.

**Pre-backup hook**: set `PRE_BACKUP_HOOK=/path/to/script.sh` to run a script before each backup. Useful for SQLite database dumps. The hook runs in the sidecar container (same filesystem as the PVC mount). A non-zero exit code is logged as an error but does not block the backup.

**Verify backups are running**:
```bash
# Check the timestamp file on the PVC
kubectl exec <pod> -c k8si-backup -- cat /data/.k8si-last-backup

# List snapshots
kubectl exec <pod> -c k8si-backup -- restic snapshots
```

---

## Environment variable reference

### Both modes

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODE` | yes | — | `restore` or `backup` |
| `DATA_PATH` | no | `/data` | Path where the PVC is mounted |
| `RESTIC_REPOSITORY` | yes | — | Full restic repository URL |
| `RESTIC_PASSWORD` | yes* | — | Restic encryption password |
| `RESTIC_PASSWORD_FILE` | yes* | — | Path to file containing the password |

*Either `RESTIC_PASSWORD` or `RESTIC_PASSWORD_FILE` must be set.

The `RESTIC_SFTP_COMMAND` env var is passed through from the secret directly to restic via `-o sftp.command=...`.

### Restore mode only (`MODE=restore`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SENTINEL_FILE` | yes | — | Path relative to `DATA_PATH` (e.g. `config.xml`) |

### Backup mode only (`MODE=backup`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BACKUP_SCHEDULE` | yes | — | Cron expression (e.g. `0 2 * * *`) |
| `RETENTION_DAILY` | no | `7` | Daily snapshots to keep |
| `RETENTION_WEEKLY` | no | `4` | Weekly snapshots to keep |
| `RETENTION_MONTHLY` | no | `3` | Monthly snapshots to keep |
| `PRE_BACKUP_HOOK` | no | — | Absolute path to pre-backup script |
| `BACKUP_TAGS` | no | — | Comma-separated tags (e.g. `app=sonarr,env=prod`) |

---

## Limitations

**No alerting.** The sidecar writes `DATA_PATH/.k8si-last-backup` on each successful cycle. Monitoring (e.g. a Prometheus rule checking `file_mtime_seconds` via node-exporter) is left to the operator.

**No stale lock cleanup.** If a backup is interrupted (node killed, OOM), restic may leave a lock. The next backup cycle will fail with `repository is already locked`. Fix manually:

```bash
kubectl exec <pod> -c k8si-backup -- restic unlock
```

**Runs as root.** The container runs as root so restic can preserve file ownership (`uid`/`gid`) on restore. Do not reduce this — non-root restore silently produces wrong permissions.

**Single backend.** Only SFTP (Hetzner Storagebox) is tested. Restic supports other backends (S3, B2, etc.) — set `RESTIC_REPOSITORY` and any backend-specific env vars. The SFTP-specific `-o sftp.command=...` flag is only injected when `RESTIC_SFTP_COMMAND` is set.

**`restic init` is manual.** The repository must be initialized before first use. There is no automatic `restic init` in the current init container. Run it once as a Job or locally.

**No restore verification.** k8si does not run `restic check` after restore. Verify periodically with `restic check` on the repository.

**K8s 1.29+ required for sidecar.** The `restartPolicy: Always` field in `initContainers` (native sidecars) was promoted to stable in 1.29. On older clusters, use a separate container in `spec.containers` instead. The `--no-sidecar` flag generates only the restore init container for this case.

---

## Operator (planned)

A Kopf-based Kubernetes operator is planned to replace the backup sidecar. See `docs/operator-plan.md` for the design.

The operator introduces a `K8siBackup` CRD. Instead of a long-running sidecar in every pod, the operator creates a CronJob per app and writes backup status back to the CRD:

```bash
kubectl get k8sibackups -A
# NAMESPACE   NAME    LAST BACKUP           RESULT    NEXT BACKUP
# downloads   sonarr  2026-05-07T02:00:00Z  success   2026-05-08T02:00:00Z
```

The restore init container stays unchanged — it lives in deployment YAML, generated once with `k8si generate --no-sidecar` and committed to git.

Migration from the sidecar approach is non-destructive: both use the same restic repository, and restic handles concurrent access via locks.

---

## Development

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest tests/ -v

# Build image locally
docker build -t k8si:dev .
```

CI runs on every push: tests on all branches, multi-arch image push to `ghcr.io/jaccoh/k8si` on `main`.
