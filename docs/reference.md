# k8si reference

Detailed reference for k8si 0.9.x. For the pitch and quick start, see the
[README](../README.md).

---

## Environment variables

### All modes

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODE` | yes | — | `restore`, `backup`, or `job` |
| `DATA_PATH` | no | `/data` | PVC mount path |
| `RESTIC_REPOSITORY` | yes | — | Full repository URL (restic or kopia syntax) |
| `RESTIC_PASSWORD` | yes* | — | Repository password |
| `RESTIC_PASSWORD_FILE` | yes* | — | Path to password file (restic backend only — the kopia backend silently ignores it, see below) |
| `RESTIC_SFTP_COMMAND` | SFTP only | — | ssh command restic uses to reach the repo (normally injected from the Secret) |
| `BACKEND_TYPE` | no | `restic` | `restic` or `kopia` |

\* Either `RESTIC_PASSWORD` or `RESTIC_PASSWORD_FILE` must be set.

### Restore mode (`MODE=restore`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RESTORE_SENTINELS` | no | — | Comma-separated sentinel file paths |
| `RESTORE_REQUIRED` | no | `false` | Fail if no snapshot found |
| `RESTORE_MAX_AGE` | no | — | Max snapshot age (e.g. `7d`, `168h`) |
| `RESTORE_SIZE_MIN` | no | — | Min snapshot size (e.g. `1Mi`, `500Ki`) |
| `RESTORE_SIZE_MAX` | no | — | Max snapshot size (e.g. `50Gi`) |
| `RESTORE_TAGS` | no | — | Comma-separated tags to filter snapshots |
| `RESTORE_SNAPSHOT` | no | — | Pin to a specific snapshot ID. Pinned restores **fail loud** when the snapshot fails the sentinel quality gate (auto-picked snapshots skip instead) |
| `SENTINEL_FILE` | no | — | Legacy single-sentinel fallback, ignored when `RESTORE_SENTINELS` is set |
| `K8SI_BACKUP_NAME` | no | — | K8siBackup CRD name to report restore status to (see [Restore status reporting](#restore-status-reporting)) |
| `K8SI_BACKUP_NAMESPACE` | no | pod namespace | Namespace of that CRD |

### Backup modes (`MODE=backup` sidecar, `MODE=job` operator Job)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BACKUP_SCHEDULE` | sidecar only | — | Cron expression, evaluated UTC |
| `RETENTION_DAILY` | no | `7` | Daily snapshots to keep |
| `RETENTION_WEEKLY` | no | `4` | Weekly snapshots to keep |
| `RETENTION_MONTHLY` | no | `3` | Monthly snapshots to keep |
| `PRE_SNAPSHOT_HOOK` | no | — | Absolute path to a script run before the VolumeSnapshot |
| `PRE_SNAPSHOT_HOOK_REQUIRED` | no | `false` | Non-zero hook exit aborts the backup (fail-closed) |
| `BACKUP_TAGS` | no | — | Comma-separated tags (e.g. `app=sonarr`) |
| `RUN_CHECK` | no | `false` | Run a repository integrity check after backup (set by the CRD's `checkAfterBackup`) |

### Operator-only

| Variable | Default | Description |
|----------|---------|-------------|
| `K8SI_IMAGE` | `ghcr.io/jaccoh/k8si:latest` | Image used for backup Jobs and restore init containers |
| `BACKEND_TYPE` | `restic` | Backend for all backups handled by this operator (per-CRD `spec.backendType` is not wired up — see [Known quirks](#known-quirks)) |

### Kopia backend

| Variable | Description |
|----------|-------------|
| `KOPIA_CONFIG_PATH` | kopia config file location inside the Job |

> **kopia caveats:** experimental. `RESTIC_PASSWORD_FILE` is silently ignored
> (only `RESTIC_PASSWORD` is forwarded), and retention (`forget`) uses a
> per-source kopia policy rather than kopia's global policy.

---

## Secret format

One Secret per app. Two keys are always required; the other three are needed
only for SFTP-backed repositories (e.g. Hetzner Storagebox):

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: restic-sonarr-config
  namespace: downloads
stringData:
  RESTIC_REPOSITORY: "sftp:u12345@u12345.your-storagebox.de:backup/sonarr-config"
  RESTIC_PASSWORD: "your-repo-password"                        # required
  RESTIC_SFTP_COMMAND: "ssh -i /restic-ssh/id_ed25519 -p 23 -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/restic-ssh/known_hosts -o HostKeyAlgorithms=ecdsa-sha2-nistp521 u12345@u12345.your-storagebox.de -s sftp"  # SFTP only
  id_ed25519: |                                                # SFTP only
    -----BEGIN OPENSSH PRIVATE KEY-----
    ...
    -----END OPENSSH PRIVATE KEY-----
  known_hosts: "u12345.your-storagebox.de ecdsa-sha2-nistp521 AAAA..."  # SFTP only
```

Mounting differs per consumer:

- **Backup Jobs** (operator): the secret's SSH keys are projected to
  `/restic-ssh/` with mode `0400` (the mount is optional — non-SFTP backends
  can omit the keys entirely).
- **Restore init containers**: a `busybox` `fix-ssh-perms` init container copies
  the keys into an emptyDir first (SSH requires the key not be
  group/world-readable); `id_ed25519` ends up `0400`, `known_hosts` `0644`.
  With an SFTP repository this path requires the key keys to exist in the
  Secret.

---

## RBAC modes

Two interchangeable setups ship in `deploy/`. Pick one at install time; they
are mutually exclusive (applying both grants the union, never the narrower set).

| | `deploy/rbac.yaml` (default) | `deploy/rbac-namespaced.yaml` |
|---|---|---|
| Secrets `get` | ClusterRole — every namespace | namespace `Role` only |
| `pods/exec` `create` | ClusterRole — every namespace | namespace `Role` only |
| New backup namespace works immediately | yes | after you apply its Role + RoleBinding |
| Blast radius of a compromised operator | read every Secret in the cluster, exec into any pod | only the namespaces you explicitly enrolled |

Everything else is identical and stays cluster-scoped in both modes, because
the operator runs `--all-namespaces` and genuinely needs it cluster-wide: the
k8si CRDs, Jobs, pod *metadata* (read-only) and logs, events, VolumeSnapshots,
PVCs and PVs.

The two grants that move are the dangerous ones. `secrets get` reads your
restic/kopia repository URL and password; `pods/exec create` is what runs
`mysqldump` / `pg_dump` / `sqlite3` inside your application pods for database
quiescing. Both are only ever exercised inside a namespace that owns a
`K8siBackup`, so in the scoped mode they live in a per-namespace `Role`
(`k8si-operator-namespace`) that you must replicate into every namespace you
back up:

```bash
# Enrol one namespace (reads the last two documents of the manifest):
sed -e 's/namespace: k8si-system/namespace: downloads/' \
    deploy/rbac-namespaced.yaml | kubectl apply -f -

# Or copy the Role + RoleBinding pair out of rbac-namespaced.yaml and edit
# metadata.namespace by hand for each namespace.
```

**The trade-off is fail-closed vs. convenient.** In the scoped mode a freshly
created backup namespace is inert until its Role and RoleBinding exist — its
backups fail with RBAC errors instead of silently working. That is deliberate:
an unreviewed namespace cannot hand the operator its Secrets. If you add
namespaces rarely and deliberately, take the scoped mode. If namespaces come
and go (or you just want `kubectl apply -f deploy/` to be the whole story),
stay on the default.

Upgrading from cluster-wide to scoped: apply `rbac-namespaced.yaml`, then
delete the old ClusterRoleBinding is *not* required — the ClusterRole is
replaced in place with the narrower rules, so the wide grants disappear on the
same object name. Do it namespace by namespace (enrol first, then flip the
ClusterRole) and a mis-enrolled namespace shows up as failed Jobs rather than
failed backups you discover weeks later.

---

## Dashboard exposure and access control

`deploy/ui.yaml` ships a NodePort Service on `:30080` — reachable from every
node on the network, no TLS, no authentication. That is the LAN-trust default.
`deploy/ui-ingress.yaml` is the variant for everything else: a ClusterIP
Service plus an Ingress, so the dashboard goes through your ingress controller
and inherits its TLS, host routing and middlewares (a commented Traefik block
is included):

```bash
kubectl apply -f deploy/ui.yaml          # SA, RBAC, Deployment, NodePort Service
kubectl apply -f deploy/ui-ingress.yaml  # flips the Service to ClusterIP + adds Ingress
```

Both Services are named `k8si-ui`, so whichever you applied last wins and there
is never a duplicate. Set a real hostname in the Ingress before applying.

> As of 0.10.0 both manifests land in `k8si-system`, the same namespace as the
> operator. (In 0.9.x `deploy/ui.yaml` targeted a separate `k8si` namespace that
> nothing else created — `kubectl apply -f deploy/` failed out of the box.)

### `K8SI_UI_TOKEN` (off by default)

The dashboard's mutating endpoints — trigger a backup now, pause, resume —
accept an optional bearer token. Unset, the dashboard is open to anyone who can
reach it. Set, every mutating call must carry a matching `X-K8si-Token` header
(compared with `hmac.compare_digest`, so timing attacks don't help):

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

The dashboard prompts for the token the first time you press Backup-now and
remembers it for the session (stored in `sessionStorage`, not a cookie). Read-only
views — status, sparkline, log drawer — stay open with or without the token;
`K8SI_UI_TOKEN` protects the buttons, not the data. To lock down the data too,
keep the dashboard behind your ingress access control.

---

## K8siBackup spec reference

```yaml
apiVersion: k8si.io/v1
kind: K8siBackup
metadata:
  name: <app>
  namespace: <namespace>
spec:
  # required
  pvc: <pvc-claim-name>          # PVC to back up, mounted at /data
                                 # IMMUTABLE — changing it would silently point
                                 # the backup at a different dataset; create a
                                 # new K8siBackup instead
  schedule: "0 2 * * *"          # Cron (UTC), five fields — validated by a
                                 # pattern; six-field and @-forms are rejected

  # backend — resticSecret (restic) or kopiaSecret (kopia)
  # all three references are IMMUTABLE: they identify which dataset and which
  # credentials the repository was built with. Rotate the Secret's contents,
  # never the reference — repointing a repo at a different PVC/secret means a
  # new K8siBackup.
  resticSecret: <secret-name>
  kopiaSecret: <secret-name>     # kopia backend
  repositoryPVC: <pvc-name>      # mount a PVC at /repo; use with
                                 # RESTIC_REPOSITORY=file:///repo for local repos
  backendType: restic            # NOT WIRED — backend comes from the
                                 # operator's BACKEND_TYPE env (see quirks)

  backupMode: snapshot           # snapshot (default) or direct
  volumeSnapshotClass: ""        # omit for cluster default (snapshot mode)

  tags: ["app=sonarr"]           # restic/kopia tags on backups

  restore:
    sentinels: ["config.xml"]    # prove data health on disk and in snapshot
    required: false              # true = fail loud if no snapshot found
    maxAge: "7d"                 # skip restore if latest snapshot older
    size:
      min: "1Mi"
      max: "50Gi"
    tags: ["app=sonarr"]         # only consider snapshots with these tags

  retention:
    daily: 7
    weekly: 4
    monthly: 3

  # Optional: quiesce a database before the snapshot
  database:
    type: mariadb                # mariadb | postgres | sqlite
    secretRef: db-credentials    # DB_HOST, DB_PORT, DB_USER, DB_PASSWORD,
                                 # DB_NAME (mariadb/postgres)
    podSelector: {}              # label selector for exec (sqlite)
    dbPaths: ["/data/app.db"]    # sqlite files inside the pod (sqlite)

  preSnapshotHook: ""            # script in the k8si image run before snapshot;
  preSnapshotHookRequired: false # true = hook failure aborts the backup

  checkAfterBackup: false        # repo integrity check after each backup

  paused: false                  # suspend ALL backups incl. manual triggers
  backupWindow:                  # UTC window; end < start wraps midnight
    start: "02:00"
    end:   "06:00"
  maxRetriesPerDay: 3            # failed scheduled runs per UTC day (manual
                                 # triggers bypass the cap)
  jobTimeout: 3600               # seconds; also Job activeDeadlineSeconds
  notifyOnSuccess: "https://hooks.example.com/ok"
  notifyOnFailure: "https://hooks.example.com/err"
  resources: {}                  # overrides backup Job pod resources
                                 # (defaults: requests 50m/128Mi, limits 200m/1Gi)
```

### Status fields (written by the operator)

| Field | Description |
|-------|-------------|
| `lastBackupResult` | `pending` / `running` / `success` / `failed` |
| `lastBackupTime` | Timestamp of the last **successful** backup (failures don't move it) |
| `lastBackupDuration` | Last run duration in seconds |
| `nextBackupTime` | Next scheduled run |
| `message` | Last error message (empty on success) |
| `lastRunRef` / `lastSuccessfulRunRef` | Most recent / most recent successful `K8siBackupRun` |
| `recentBackups` | Rolling last 30 results — `[{time, result}]` |
| `recentRuns` | Rolling last 30 runs with artifact metadata (name, snapshotId, sizeBytes, backendType) — feeds the dashboard sparkline |
| `lastRunLog` | Phase log of the most recent run, cleared at run start |
| `triggeredAt` | Set to trigger a manual backup; cleared when the run is created |
| `restorePatch` | Init container YAML to paste into your pod spec |

### Restore status reporting

`lastRestoreResult` (`success` / `failed` / `skipped`), `lastRestoreTime`,
`lastRestoreSnapshotId`, and `lastRestoreMessage` are written by the restore
init container — but **only when it is configured for reporting**. To get
restore status on the CRD you need all of:

1. `K8SI_BACKUP_NAME` / `K8SI_BACKUP_NAMESPACE` env on the init container
   (`k8si generate --backup-name <name>` adds them). `build_restore_patch`
   emits them whenever it is given the CR's `name`/`namespace`; without a name
   the vars are omitted and reporting stays off. The operator call sites in
   `k8si/operator/main.py` still call it with `spec` only, so until that
   one-argument change lands the generated `restorePatch` does **not** carry
   them — `k8si generate --backup-name` is the wired path today.
2. the `k8si-restore` ClusterRole from `deploy/rbac.yaml` bound to a
   ServiceAccount the pod uses. Nothing in `deploy/` binds it for you, because
   the binding decides *which* pods may write status — that is a per-app call.
   Add this to the app's namespace when you want reporting:

   ```yaml
   apiVersion: rbac.authorization.k8s.io/v1
   kind: ClusterRoleBinding
   metadata:
     name: k8si-restore-<app>
   roleRef:
     apiGroup: rbac.authorization.k8s.io
     kind: ClusterRole
     name: k8si-restore
   subjects:
     - kind: ServiceAccount
       name: <app-serviceaccount>
       namespace: <app-namespace>
   ```

   The ClusterRole itself is deliberately minimal — a single rule granting
   `patch` on `k8sibackups/status` and nothing else, so a compromised app pod
   can append restore results but cannot read Secrets, cannot touch `spec`, and
   cannot reach any other resource. Prefer a `RoleBinding` in the app's
   namespace instead if all of that app's K8siBackups live there; the
   ClusterRoleBinding form is only needed when the pod reports to a CR in
   another namespace.

Without both, restore status fields simply never populate.

---

## K8siBackupRun reference

Created by the operator timer (schedule), the manual-trigger path
(`status.triggeredAt`), or the dashboard (direct create). You can also create
one by hand:

```yaml
apiVersion: k8si.io/v1
kind: K8siBackupRun
metadata:
  name: sonarr-config-20260707020000
  namespace: downloads
  labels:
    k8si.io/backup: sonarr-config
spec:
  backupRef: sonarr-config     # parent K8siBackup
  triggeredBy: manual          # manual | schedule | backfill
  triggeredAt: "2026-07-07T02:00:00Z"
  mode: snapshot               # snapshot | direct — overrides parent backupMode
```

`triggeredBy: backfill` runs are recorded but deliberately not executed.
Status: `phase` (`Pending` → `Running` → `Succeeded`/`Failed`), `startTime`,
`completionTime`, `message`, `snapshotId`, `sizeBytes`, `backendType`, and an
append-only `log` of `[{time, phase, message}]` entries.

The operator also reconciles runs: a run stuck `Pending` > 5 min or `Running`
> 60 min is failed and its orphaned Job deleted.

Printer columns: `kubectl get k8sibackupruns` shows `BACKUP`, `PHASE`,
`STARTED`, `COMPLETED`, `BY`. Short names: `k8b`, `k8br`.

---

## Backend plugins

The `BackupBackend` protocol in [`k8si/backend.py`](../k8si/backend.py) defines
the interface. Two implementations ship:

| Backend | Module | Selected when |
|---------|--------|---------------|
| **restic** (default) | `k8si/backends/restic.py` | `BACKEND_TYPE` unset or `restic` |
| **kopia** (experimental) | `k8si/backends/kopia.py` | `BACKEND_TYPE=kopia` |

Core methods: `init()`, `unlock()`, `check()`, `snapshots(tags)`, `ls(id)`,
`snapshot_size(id)`, `check_sentinels(id, sentinels)`, `verify_snapshot(tag)`,
`restore(id)`, `backup(source, tags)`, `forget(daily, weekly, monthly, prune)`.
Errors raise `BackupError`; an empty repository raises `NoSnapshotsError`. See
the module for exact signatures — the restore quality gate lives in
`check_sentinels`.

The backup cycle auto-initializes the repository on first use, proactively
unlocks stale restic locks at the start of each cycle, and retries once after
unlocking if a backup/forget hits a lock error — manual `restic unlock` is only
needed if a Job dies mid-write twice.

---

## Known quirks

- **`spec.backendType` is not wired.** The field exists in the CRD but the
  operator only reads its own `BACKEND_TYPE` env. Setting `backendType: kopia`
  on a restic-configured operator silently does a restic backup.
- **`.k8si-last-backup` marker.** The backup cycle writes a
  `DATA_PATH/.k8si-last-backup` timestamp — useful for file-mtime monitoring in
  `direct` mode or the legacy sidecar. In `snapshot` mode the Job writes it to
  the ephemeral clone, which is deleted afterwards, so the marker on your live
  PVC never updates.
- **Age/size bounds apply to the latest snapshot only.** If the newest snapshot
  is out of bounds, restore skips — it does not search older candidates.
