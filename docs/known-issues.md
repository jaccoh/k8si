# Known Issues / Operator Improvements

## 1. restore.tags must match backup tags (or be omitted)

**Symptom**: restore init container fails with "no snapshots found" even though backups are succeeding.

**Root cause**: `spec.tags` tags the backup snapshots. `spec.restore.tags` filters which snapshots are eligible for restore. If they differ (or restore.tags is set but spec.tags is not), no snapshots match.

**Fix needed**: the operator should default `restore.tags` to `spec.tags` when `restore.tags` is not explicitly provided. A user setting `spec.tags: [app=foo]` almost certainly wants restore to filter by the same tag.

## 2. Restore init container must mount PVC at /data, not at the app's native path

**Symptom**: restore runs without error but post-restore sentinel check fails ("sentinels still missing after restore"). Pod loops in Init:Error / CrashLoopBackOff.

**Root cause**: the backup CronJob always mounts the PVC at `/data` (k8si default). Restic stores paths as `/data/...`. The restore target is `/`, so restored files land at `/data/...`. If the restore init container mounts the PVC at a different path (e.g. `/var/lib/postgresql/data` copied from the app container), the restored data writes to the container overlay layer, not the PVC. Nothing is persisted.

**Fix needed**: the operator-generated `restorePatch` should always emit `mountPath: /data` for the restore init container's PVC volume, regardless of where the app itself mounts the volume. The operator knows DATA_PATH; it should use it.

**Workaround**: always use `mountPath: /data` in the restore init container volumeMount.
