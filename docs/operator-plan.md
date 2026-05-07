# k8si Operator — Design Plan

## Scope

A Kopf-based Kubernetes operator that owns backup scheduling via a `K8siBackup`
CRD. Restore stays explicit in deployment YAML (init container, committed to git).
The operator does not mutate deployments or use a webhook.

---

## Why an Operator (and why not)

**Worth it because:**
- One `K8siBackup` CRD per app instead of a per-app CronJob manifest
- Operator writes backup status back to the CRD — observable with `kubectl get k8sibackups`
- Centrally upgradable: update the operator, all backup jobs get the new image version
- Stateless: all state lives in Kubernetes CRDs, restart is free

**Explicitly not doing:**
- No mutating webhook — restore init containers stay in deployment YAML, in git
- No admission controller — no magic, no hidden pod modifications
- No operator-managed PVC discovery — you declare the PVC explicitly in the CRD
- No cluster-scoped resources beyond the operator itself

Bootstrap risk: if the operator is down, apps start on empty PVCs (same as Velero).
On a fresh cluster this is fine — no data to restore yet. On a migration, deploy
the operator (ArgoCD wave 0) before apps (wave 1). Acceptable.

---

## CRD: K8siBackup

```yaml
apiVersion: k8si.io/v1
kind: K8siBackup
metadata:
  name: sonarr
  namespace: downloads
spec:
  pvc: sonarr-config          # PVC to back up
  resticSecret: restic-sonarr-config  # Secret with RESTIC_REPOSITORY, RESTIC_PASSWORD, SSH key
  schedule: "0 2 * * *"       # cron, same format as k8si backup sidecar
  retention:
    daily: 7
    weekly: 4
    monthly: 3
  preBackupHook: /hooks/sqlite-backup.sh  # optional, path in the backup pod
  resources:
    requests:
      cpu: 50m
      memory: 64Mi
    limits:
      cpu: 200m
      memory: 256Mi
status:
  lastBackupTime: "2026-05-07T02:00:00Z"
  lastBackupResult: success     # success | failed
  nextBackupTime: "2026-05-08T02:00:00Z"
  message: ""
```

`K8siBackup` is namespace-scoped. One per app. The PVC and the secret must be
in the same namespace.

---

## What the Operator Does

For each `K8siBackup` resource the operator:

1. **Creates a CronJob** in the same namespace that runs `k8si` in backup mode.
   The CronJob mounts the PVC and the restic secret. The `k8si` image version
   is taken from the operator's own config (one place to update for all apps).

2. **Watches the CronJob's Jobs** — on completion, reads success/failure and
   writes `status.lastBackupTime` and `status.lastBackupResult` back to the CRD.

3. **On CRD delete** — deletes the owned CronJob (owner references handle this
   automatically).

4. **On CRD update** — reconciles: updates the CronJob schedule/resources/image.

The operator owns the CronJob. The CronJob owns the Jobs. Standard Kubernetes
garbage collection handles cleanup.

---

## What the Operator Does NOT Do

- Does not inject init containers into deployments
- Does not patch or watch existing Deployments/StatefulSets
- Does not manage the restic repository (init is still manual or a one-time Job)
- Does not handle restore — that stays as an explicit init container in the
  deployment manifest

Restore init container example (unchanged from current design, lives in git):

```yaml
initContainers:
  - name: k8si-restore
    image: ghcr.io/jaccoh/k8si:1.0.0
    env:
      - name: MODE
        value: restore
      - name: SENTINEL_FILE
        value: config.xml
      - name: RESTIC_REPOSITORY
        valueFrom:
          secretKeyRef:
            name: restic-sonarr-config
            key: RESTIC_REPOSITORY
      - name: RESTIC_PASSWORD
        valueFrom:
          secretKeyRef:
            name: restic-sonarr-config
            key: RESTIC_PASSWORD
    volumeMounts:
      - name: sonarr-config
        mountPath: /data
      - name: restic-ssh
        mountPath: /restic-ssh
        readOnly: true
```

---

## Implementation: Kopf

```
k8si/
└── operator/
    ├── __init__.py
    ├── main.py          # kopf.on.create / update / delete handlers
    ├── cronjob.py       # CronJob template builder
    └── status.py        # status patch helpers
```

Kopf handles:
- Leader election (safe multi-replica operator)
- CRD watch and event dispatch
- Retry on transient errors
- Status patching

The operator is packaged in the same `k8si` image (different entrypoint:
`kopf run k8si/operator/main.py`). No separate image needed.

---

## Deployment

```yaml
# One Deployment in the k8si-system namespace
# ClusterRole to read/write K8siBackup CRDs and manage CronJobs in any namespace
# No webhook, no cert-manager dependency
```

ArgoCD sync wave 0 (before apps). The operator itself has no PVC dependency
so it starts immediately on a fresh cluster.

---

## Observability

```bash
kubectl get k8sibackups -A
# NAMESPACE   NAME      LAST BACKUP           RESULT    NEXT BACKUP
# downloads   sonarr    2026-05-07T02:00:00Z  success   2026-05-08T02:00:00Z
# nextcloud   data      2026-05-07T02:00:00Z  success   2026-05-08T02:00:00Z
```

Alerting: a Prometheus rule checks `k8sibackup_last_success_age_seconds > 2 * interval`.
The operator exposes this metric or a liveness file approach works too
(same `.k8si-last-backup` file the current sidecar writes).

---

## Migration from current sidecar approach

If the sidecar backup is already running in a pod:
1. Apply `K8siBackup` CRD for the app
2. Operator creates the CronJob
3. Remove the `k8si-backup` sidecar from the deployment (redeploy)
4. Both approaches use the same restic repo — no data migration

Transition is safe: restic handles concurrent access via locks.

---

## Open Questions

1. **Image version pinning**: how does the operator know which `k8si` image version
   to use for CronJobs? Options: operator env var `K8SI_IMAGE`, or a field on the
   CRD. Env var is simpler — one place to update via Renovate.

2. **Repo init**: first deploy, restic repo doesn't exist yet. The operator could
   run `restic init` as part of CronJob creation. Or leave it manual (current
   approach). Operator-managed init is cleaner for the "fresh cluster" story.

3. **Check and prune scheduling**: current design runs forget/prune after every
   backup. The operator could add separate `check` scheduling (weekly integrity
   check) consistent with what k8up offered.
