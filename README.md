# k8si

[![Release](https://img.shields.io/github/v/release/jaccoh/k8si)](https://github.com/jaccoh/k8si/releases)
[![Arch](https://img.shields.io/badge/arch-amd64%20%7C%20arm64-blue)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Backup is a solved problem. Restore isn't.**

GitOps can rebuild your cluster from a git repo in minutes — everything except the data. k8si closes that gap. Every pod gets an init container that asks one question at startup: *is my data here?* If the sentinel files are on disk, it exits in milliseconds without touching the repository. If the volume is empty — fresh PVC, deleted namespace, botched migration — it finds the latest good snapshot and restores it before the app ever starts. And when it can't restore safely, the pod refuses to boot on empty state instead of silently starting broken.

**GitOps rebuilt your cluster. k8si extends that to your data.**

---

## How it works

You declare backups as a `K8siBackup` resource next to the PVC. On schedule the operator creates a `K8siBackupRun` that drives the pipeline:

```
(optional) DB quiesce → pre-snapshot hook → CSI VolumeSnapshot → ephemeral PVC clone → restic backup Job → cleanup
```

In the default `snapshot` mode the backup job never mounts your live PVC — it works from a point-in-time clone. (Exceptions: a `preSnapshotHook` or SQLite quiescing interact with the live volume/app by design.) In `direct` mode the backup job mounts the live PVC itself. `kubectl get k8sibackups -A` shows backup health for every app in the cluster.

| Component | What it does |
|-----------|--------------|
| **Restore init container** | Checks sentinel files on the PVC at every pod start; restores from the backup backend if data is missing or incomplete. Fails loud when it can't restore safely. |
| **Operator** | Kopf-based. A 60s timer turns each `K8siBackup` schedule into a `K8siBackupRun` and drives the pipeline, with retry caps, backup windows, timeouts, and watchdogs for stuck runs. |
| **Dashboard** | FastAPI web UI: status across namespaces, run sparkline, live log drawer, Backup-now and pause/resume buttons. NodePort `:30080` or Ingress (`deploy/ui-ingress.yaml`). Mutating endpoints take an optional `K8SI_UI_TOKEN` — see [Dashboard access control](#dashboard-access-control). |

Backend is pluggable: **restic** (default, SFTP to e.g. Hetzner Storagebox out of the box) and **kopia** (experimental). Images: `ghcr.io/jaccoh/k8si` for `linux/amd64` and `linux/arm64`.

## The restore story

The init container runs once per pod start and makes exactly one decision:

| Condition | What happens | App sees |
|-----------|--------------|----------|
| `.k8si-no-restore` file on the PVC | Skip — emergency override, no git commit needed | whatever is on disk |
| All sentinels present on disk | Skip — data is healthy, no repo access | normal startup |
| Restore marker present, sentinels missing | **Fail loud** — post-restore corruption detected | pod stays in `Init:Error` |
| No sentinels configured, marker present | Skip — already initialized | normal startup |
| No snapshots, `restore.required: false` | Skip — first deploy | empty volume, fresh start |
| No snapshots, `restore.required: true` | **Fail loud** | pod stays in `Init:Error` |
| Snapshot missing the sentinels (quality gate) | Skip — the backup looks wrong, don't trust it ¹ | empty volume, fresh start |
| Snapshot outside age/size bounds | Skip ¹ | empty volume, fresh start |
| Restore succeeds, sentinels appear | Write `.k8si-restore-complete` marker | restored data |
| Restore fails | **Fail loud** | pod stays in `Init:Error` |

¹ When a snapshot is pinned explicitly (`RESTORE_SNAPSHOT`), a failed quality gate fails loud instead of skipping. Bounds apply to the latest snapshot only — k8si does not search older candidates.

**Sentinels are written by your app, not by k8si.** They mark "this app fully initialized its data" — Sonarr writes `config.xml`, Nextcloud writes `config/config.php`. k8si checks the sentinel exists in the snapshot *before* restoring (quality gate) and on disk *after* restoring. A restore that comes back without the sentinels is treated as corruption, not success.

So the disaster movie looks like this: the cluster dies. GitOps rebuilds every Deployment. Every pod starts, its init container finds an empty PVC, restores the latest snapshot that passes the gates, writes the marker, and hands the pod over with its data intact. Nobody opens a runbook.

## Requirements

- A Kubernetes cluster and a PVC worth backing up. `snapshot` mode additionally needs the [external snapshot controller](https://github.com/kubernetes-csi/external-snapshotter), the VolumeSnapshot CRDs, and a CSI driver with snapshot support. `direct` mode needs none of those.
- A restic repository the cluster can reach — SFTP (Hetzner Storagebox is the tested default), S3, B2, or any restic-supported backend.
- Standalone sidecar mode (below) needs K8s 1.29+ for native sidecars; the operator path doesn't.

## Quick start (operator mode)

### 1. Install the CRDs, operator, and (optionally) the dashboard

```bash
kubectl apply -f https://raw.githubusercontent.com/jaccoh/k8si/main/deploy/crd.yaml
kubectl apply -f https://raw.githubusercontent.com/jaccoh/k8si/main/deploy/crd_run.yaml
kubectl apply -f https://raw.githubusercontent.com/jaccoh/k8si/main/deploy/rbac.yaml
kubectl apply -f https://raw.githubusercontent.com/jaccoh/k8si/main/deploy/operator.yaml

# Optional dashboard (ClusterIP + Ingress instead of NodePort):
# kubectl apply -f https://raw.githubusercontent.com/jaccoh/k8si/main/deploy/ui-ingress.yaml
kubectl apply -f https://raw.githubusercontent.com/jaccoh/k8si/main/deploy/ui.yaml
```

> `deploy/rbac.yaml` grants the operator cluster-wide `secrets get` and
> `pods/exec` so any namespace can be backed up out of the box. If you would
> rather enrol namespaces one by one, use `deploy/rbac-namespaced.yaml` — see
> [RBAC modes](docs/reference.md#rbac-modes) for the trade-off.

### 2. Create the backend secret

Two keys are required; the SSH keys are needed only for SFTP repositories:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: restic-sonarr-config
  namespace: downloads
stringData:
  RESTIC_REPOSITORY: "sftp:u12345@u12345.your-storagebox.de:backup/sonarr-config"
  RESTIC_PASSWORD: "your-repo-password"
  RESTIC_SFTP_COMMAND: "ssh -i /restic-ssh/id_ed25519 -p 23 -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/restic-ssh/known_hosts -o HostKeyAlgorithms=ecdsa-sha2-nistp521 u12345@u12345.your-storagebox.de -s sftp"  # SFTP only
  id_ed25519: |          # SFTP only
    -----BEGIN OPENSSH PRIVATE KEY-----
    ...
    -----END OPENSSH PRIVATE KEY-----
  known_hosts: "u12345.your-storagebox.de ecdsa-sha2-nistp521 AAAA..."         # SFTP only
```

### 3. Create a K8siBackup

```yaml
apiVersion: k8si.io/v1
kind: K8siBackup
metadata:
  name: sonarr-config
  namespace: downloads
spec:
  pvc: sonarr-config
  resticSecret: restic-sonarr-config
  schedule: "0 2 * * *"            # UTC
  restore:
    sentinels: ["config.xml"]
    required: false
  retention:
    daily: 7
    weekly: 4
    monthly: 3

  # optional extras — full spec in docs/reference.md
  # backupMode: snapshot           # or: direct
  # database:                      # app-consistent snapshots via DB quiescing
  #   type: mariadb                #   mariadb | postgres | sqlite
  #   secretRef: db-credentials
  # backupWindow: { start: "02:00", end: "06:00" }
  # maxRetriesPerDay: 3
  # jobTimeout: 3600
  # notifyOnFailure: "https://hooks.example.com/err"
```

The repository is initialized automatically on first backup — no manual init job.

### 4. Check status

```bash
kubectl get k8sibackups -A          # short name: k8b
```
```
NAMESPACE   NAME            SCHEDULE    LAST BACKUP           RESULT    DURATION   NEXT BACKUP           PAUSED
downloads   sonarr-config   0 2 * * *   2026-05-08T02:00:00Z  success   94         2026-05-09T02:00:00Z  false
```

Every run is also a `K8siBackupRun` object with a phase log you can watch:
`kubectl get k8sibackupruns -n downloads -w`.

### 5. Add restore to your pods

The operator generates the init container YAML and stores it in `.status.restorePatch`:

```bash
kubectl get k8sibackup sonarr-config -n downloads -o jsonpath='{.status.restorePatch}'
```

Paste that into your Deployment's `initContainers` (mounting the same PVC at `/data`), or generate it offline:

```bash
docker run --rm ghcr.io/jaccoh/k8si:latest generate \
  --app sonarr --pvc sonarr-config --secret restic-sonarr-config \
  --sentinel config.xml --no-sidecar
```

`k8si generate` without `--no-sidecar` emits a **standalone mode** — the same restore init container plus a native-sidecar backup scheduler (K8s 1.29+), no operator or CRDs required. Don't combine the sidecar with an operator-managed `K8siBackup` for the same PVC, or you'll double-schedule backups.

## Dashboard access control

The dashboard ships open — anyone who can reach it can trigger or pause backups.
Two knobs, usable together:

**`K8SI_UI_TOKEN`** (since 0.9.0, off by default) puts a bearer token on the
mutating endpoints (Backup-now, pause, resume). Every mutating call must then
carry a matching `X-K8si-Token` header; the dashboard prompts for it the first
time you press a button and remembers it for the session. Read-only views stay
open.

```bash
kubectl -n k8si-system create secret generic k8si-ui-token \
  --from-literal=token="$(openssl rand -hex 32)"
```

```yaml
# add to the ui container env in deploy/ui.yaml
- name: K8SI_UI_TOKEN
  valueFrom:
    secretKeyRef: {name: k8si-ui-token, key: token}
```

**`deploy/ui-ingress.yaml`** replaces the `:30080` NodePort with a ClusterIP
Service plus an Ingress, so the dashboard sits behind your ingress controller's
TLS, host routing and middlewares instead of a hole on every node:

```bash
kubectl apply -f deploy/ui.yaml          # SA, RBAC, Deployment, NodePort Service
kubectl apply -f deploy/ui-ingress.yaml  # flips the Service to ClusterIP + Ingress
```

Set a real hostname in the Ingress before applying. Details and a commented
Traefik block: [docs/reference.md](docs/reference.md#dashboard-exposure-and-access-control).

## Manual trigger, pause, windows

Trigger a backup outside the schedule by patching `status.triggeredAt`:

```bash
kubectl patch k8sibackup sonarr-config -n downloads \
  --type=merge --subresource=status \
  -p '{"status": {"triggeredAt": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}}'
```

The operator picks it up within 60s, bypassing the schedule check, the backup window, and the daily retry cap. `paused: true` blocks everything, including this path. The dashboard's **Backup now** button is more direct: it creates the `K8siBackupRun` immediately (409 if a run is already active). The button is disabled while paused, though the dashboard API does not itself re-check `paused` — one of the reasons it needs network restriction (see below).

## Monitoring & notifications

- **Webhooks:** `notifyOnSuccess` / `notifyOnFailure` receive a JSON POST (`name`, `namespace`, `result`, `message`, `time`, `duration`) after every run.
- **Prometheus:** the operator exposes `k8si_backup_last_success_timestamp_seconds`, `k8si_backup_result`, and `k8si_backup_duration_seconds` on port 8000. The shipped manifests don't create a Service for it — add one or scrape at pod level.
- **kubectl:** `lastBackupResult`, `recentBackups` (last 30), and per-run logs are on the CRD; the dashboard renders them.

## How it compares

| | k8si | Velero | K8up | VolSync |
|---|---|---|---|---|
| Restore trigger | **pod start, automatic** | manual command | manual `Restore` object | manual / sync job |
| Scope | PVC data (+ DB quiesce) | cluster state + volumes | PVC data | PVC replication |
| Backend | restic, kopia | restic + plugins | restic | rsync/rclone/restic |
| DB quiescing | MariaDB, Postgres, SQLite | via hooks | via commands | — |
| Sweet spot | homelab / small clusters | enterprise DR | restic-native shops | cross-cluster sync |

**What k8si is not:** not cluster-state backup — your GitOps repo owns Deployments and ConfigMaps, k8si owns the data on PVCs. Not replication or HA — restores pull from restic on pod start; there's no continuous sync. Not for multi-writer volumes — one PVC, one writer, and the sentinel gate assumes it.

## Limitations & security notes

- **Single-node assumption in snapshot mode**: backup Jobs are pinned to the node the PVC lives on and snapshot clones must land on that same node — with node-local storage (topolvm) that effectively means one storage node. On multi-node clusters use `backupMode: direct` with an object-storage repository (S3/B2) instead.


- **The dashboard is unauthenticated by default** and can trigger and pause backups cluster-wide, exposed on a NodePort on every node. Restrict it (NetworkPolicy, firewall, or an auth proxy), switch to the Ingress variant, or set [`K8SI_UI_TOKEN`](#dashboard-access-control).
- **Backup jobs and restore containers run as root.** Restore must preserve file ownership; a non-root restore produces wrong permissions silently.
- **The operator RBAC is broad by default** (cluster-wide watches, secret reads, pod exec for DB quiescing) so that backing up a new namespace needs no RBAC step. `deploy/rbac-namespaced.yaml` narrows `secrets get` and `pods/exec` to a per-namespace Role you enrol explicitly — see [RBAC modes](docs/reference.md#rbac-modes).
- **VolumeSnapshot conflicts:** if another system (e.g. VolSync) is snapshotting the same PVC, k8si waits up to 30 minutes (polling every 60s) for the conflict to clear. If it never clears the run *fails* — which counts toward `maxRetriesPerDay`.
- **Single-writer PVCs**; the sentinel quality gate needs at least one sentinel file to be meaningful.
- **kopia is experimental** (see [docs/reference.md](docs/reference.md) for caveats).

## FAQ

**My pod is stuck in `Init:Error`.** That's the fail-loud path working — k8si refused to boot the app on state it doesn't trust. `kubectl logs <pod> -c k8si-restore` says exactly why (no snapshot + `required: true`, post-restore corruption, or a failed restore).

**Does it hit the backup repo on every restart?** No. Sentinel files present → the init container exits without any repository access.

**Which snapshot gets restored?** The latest one that passes the quality gate and bounds. Pin a specific one with `RESTORE_SNAPSHOT` (pinned restores fail loud instead of skipping).

**Why did my app start with empty data?** A skip path fired — stale/too-small snapshot, failed quality gate, or `required: false` on a first deploy with no snapshots. The init container log states which.

**Am I locked in?** No. Repositories are plain restic (or kopia) repos — the stock CLI can read and restore them without k8si ever being installed.

## Status & support

Solo-maintained, running in production on the author's own cluster (ArgoCD-managed, mixed amd64/arm64 nodes). 0.9.x is a correctness- and security-hardening cycle. Issues are welcome; there is no SLA. MIT license — see [LICENSE](LICENSE).

## Development

```bash
uv sync
uv run pytest tests/ -v
docker build -t k8si:dev .
```

CI (lint, unit tests, multi-arch builds, e2e against a real cluster) runs in the maintainer's Gitea instance on every push to `main`, and publishes the public multi-arch images to `ghcr.io/jaccoh/k8si`. GitHub is the open-source home: releases appear there when a `v*` tag is pushed.

Detailed reference — full CRD spec/status, environment variables, secret format, backend plugin protocol, known quirks — lives in [docs/reference.md](docs/reference.md).
