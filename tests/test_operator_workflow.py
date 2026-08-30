"""Tests for k8si/operator/workflow.py and Kopf event logging."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import kubernetes.client.exceptions
import pytest

from k8si.operator.workflow import (
    _cleanup_orphan_snap_pvcs,
    _wait_job_complete_sync,
    run_backup,
)


def test_run_backup_emits_kopf_events() -> None:
    spec = {
        "pvc": "test-pvc",
        "resticSecret": "test-secret",
        "schedule": "0 2 * * *",
        "database": {"type": "postgres", "secretRef": "db-secret"},
        "preSnapshotHook": "/usr/local/lib/k8si/db-dump.sh",
    }
    body = {"metadata": {"name": "test-backup", "namespace": "default"}}

    with (
        patch("k8si.operator.workflow._cleanup_orphan_snap_pvcs", new_callable=AsyncMock),
        patch("k8si.operator.workflow.quiesce.quiesce_context") as mock_quiesce_ctx,
        patch("k8si.operator.workflow.snapshot.create_snapshot", new_callable=AsyncMock),
        patch("k8si.operator.workflow.snapshot.create_pvc_from_snapshot", new_callable=AsyncMock),
        patch("k8si.operator.workflow.snapshot.delete_snapshot_and_pvc", new_callable=AsyncMock),
        patch("k8si.operator.workflow._find_pvc_node_sync", return_value="node1"),
        patch("k8si.operator.workflow._run_job", new_callable=AsyncMock, return_value=""),
        patch("k8si.operator.workflow._run_hook_job", new_callable=AsyncMock),
        patch("k8si.operator.workflow.kopf.event") as mock_kopf_event,
    ):
        mock_quiesce_ctx.return_value = MagicMock()

        result = asyncio.run(run_backup("test-backup", "default", spec, MagicMock(), body))

        assert result["lastBackupResult"] == "success"

        event_calls = [call[1]["reason"] for call in mock_kopf_event.call_args_list]
        assert "QuiesceStarted" in event_calls
        assert "HookStarted" in event_calls
        assert "SnapshotStarted" in event_calls
        assert "SnapshotCreated" in event_calls
        assert "BackupJobStarted" in event_calls
        assert "BackupJobCompleted" in event_calls


def test_run_backup_direct_mode() -> None:
    spec = {
        "pvc": "test-pvc",
        "resticSecret": "test-secret",
        "schedule": "0 2 * * *",
        "backupMode": "direct",
    }
    body = {"metadata": {"name": "test-backup", "namespace": "default"}}

    _snap = "k8si.operator.workflow.snapshot"
    with (
        patch("k8si.operator.workflow._cleanup_orphan_snap_pvcs", new_callable=AsyncMock),
        patch("k8si.operator.workflow.quiesce.quiesce_context") as mock_quiesce_ctx,
        patch(f"{_snap}.create_snapshot", new_callable=AsyncMock) as mock_create_snap,
        patch(f"{_snap}.create_pvc_from_snapshot", new_callable=AsyncMock) as mock_create_pvc,
        patch(f"{_snap}.delete_snapshot_and_pvc", new_callable=AsyncMock) as mock_delete_snap_pvc,
        patch("k8si.operator.workflow._find_pvc_node_sync", return_value="node1"),
        patch(
            "k8si.operator.workflow._run_job", new_callable=AsyncMock, return_value=""
        ) as mock_run_job,
        patch("k8si.operator.workflow.kopf.event"),
    ):
        mock_quiesce_ctx.return_value = MagicMock()

        result = asyncio.run(run_backup("test-backup", "default", spec, MagicMock(), body))

        assert result["lastBackupResult"] == "success"

        mock_create_snap.assert_not_called()
        mock_create_pvc.assert_not_called()
        mock_delete_snap_pvc.assert_not_called()

        mock_run_job.assert_called_once()
        job_body = mock_run_job.call_args[0][0]
        pvc_spec = job_body["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]
        assert pvc_spec["claimName"] == "test-pvc"


# ── on_job_created callback (queued → running flip at Job start) ──────────────


def test_run_backup_invokes_on_job_created_snapshot_mode() -> None:
    """The on_job_created callback must fire when the backup Job starts — not
    at completion: main.py uses it to flip run+parent status to running and to
    record jobName while the Job is live (#5 + queued-status work)."""
    spec = {"pvc": "test-pvc", "resticSecret": "test-secret", "schedule": "0 2 * * *"}
    body = {"metadata": {"name": "test-backup", "namespace": "default"}}
    seen: list[str] = []

    async def _on_created(job_name: str) -> None:
        seen.append(job_name)

    with (
        patch("k8si.operator.workflow._cleanup_orphan_snap_pvcs", new_callable=AsyncMock),
        patch("k8si.operator.workflow.quiesce.quiesce_context") as mock_quiesce_ctx,
        patch("k8si.operator.workflow.snapshot.create_snapshot", new_callable=AsyncMock),
        patch("k8si.operator.workflow.snapshot.create_pvc_from_snapshot", new_callable=AsyncMock),
        patch("k8si.operator.workflow.snapshot.delete_snapshot_and_pvc", new_callable=AsyncMock),
        patch("k8si.operator.workflow._find_pvc_node_sync", return_value="node1"),
        patch("k8si.operator.workflow._run_job", new_callable=AsyncMock, return_value=""),
        patch("k8si.operator.workflow.kopf.event"),
    ):
        mock_quiesce_ctx.return_value = MagicMock()

        result = asyncio.run(
            run_backup(
                "test-backup", "default", spec, MagicMock(), body, on_job_created=_on_created
            )
        )

    assert seen == [result["jobName"]], "callback must fire exactly once, with the Job name"
    assert seen[0].startswith("k8si-test-backup-")


def test_run_backup_invokes_on_job_created_direct_mode() -> None:
    """Same flip in direct mode: the Job is created without a snapshot, but
    the callback timing (at Job start) is identical."""
    spec = {
        "pvc": "test-pvc",
        "resticSecret": "test-secret",
        "schedule": "0 2 * * *",
        "backupMode": "direct",
    }
    body = {"metadata": {"name": "test-backup", "namespace": "default"}}
    seen: list[str] = []

    async def _on_created(job_name: str) -> None:
        seen.append(job_name)

    with (
        patch("k8si.operator.workflow._cleanup_orphan_snap_pvcs", new_callable=AsyncMock),
        patch("k8si.operator.workflow.quiesce.quiesce_context") as mock_quiesce_ctx,
        patch("k8si.operator.workflow.snapshot.create_snapshot", new_callable=AsyncMock),
        patch("k8si.operator.workflow.snapshot.create_pvc_from_snapshot", new_callable=AsyncMock),
        patch("k8si.operator.workflow.snapshot.delete_snapshot_and_pvc", new_callable=AsyncMock),
        patch("k8si.operator.workflow._find_pvc_node_sync", return_value="node1"),
        patch("k8si.operator.workflow._run_job", new_callable=AsyncMock, return_value=""),
        patch("k8si.operator.workflow.kopf.event"),
    ):
        mock_quiesce_ctx.return_value = MagicMock()

        asyncio.run(
            run_backup(
                "test-backup", "default", spec, MagicMock(), body, on_job_created=_on_created
            )
        )

    assert len(seen) == 1 and seen[0].startswith("k8si-test-backup-")


# ── Bug 1: OOMKill detection ───────────────────────────────────────────────────


def _make_oomkill_pod(exit_code: int = 137, reason: str = "OOMKilled") -> MagicMock:
    pod = MagicMock()
    cs = MagicMock()
    cs.state.terminated.exit_code = exit_code
    cs.state.terminated.reason = reason
    cs.last_state.terminated = None
    pod.status.container_statuses = [cs]
    return pod


def _make_failed_job() -> MagicMock:
    job = MagicMock()
    job.status.succeeded = 0
    job.status.failed = 1
    return job


def test_oomkill_raises_with_descriptive_message() -> None:
    with (
        patch("k8si.operator.workflow.kubernetes.client.BatchV1Api") as mock_batch_cls,
        patch("k8si.operator.workflow.kubernetes.client.CoreV1Api") as mock_v1_cls,
    ):
        mock_batch_cls.return_value.read_namespaced_job.return_value = _make_failed_job()
        mock_v1_cls.return_value.list_namespaced_pod.return_value.items = [_make_oomkill_pod()]

        with pytest.raises(RuntimeError, match="OOMKill"):
            _wait_job_complete_sync("test-job", "default", 60)


def test_generic_failure_raises_with_exit_code() -> None:
    pod = _make_oomkill_pod(exit_code=1, reason="Error")
    with (
        patch("k8si.operator.workflow.kubernetes.client.BatchV1Api") as mock_batch_cls,
        patch("k8si.operator.workflow.kubernetes.client.CoreV1Api") as mock_v1_cls,
    ):
        mock_batch_cls.return_value.read_namespaced_job.return_value = _make_failed_job()
        mock_v1_cls.return_value.list_namespaced_pod.return_value.items = [pod]

        with pytest.raises(RuntimeError) as exc_info:
            _wait_job_complete_sync("test-job", "default", 60)
        assert "OOMKill" not in str(exc_info.value)


# ── Bug 3: Orphan snapshot PVC cleanup ────────────────────────────────────────


def test_orphan_snap_pvcs_deleted_before_backup() -> None:
    spec = {
        "pvc": "mydata",
        "resticSecret": "test-secret",
        "schedule": "0 2 * * *",
    }
    body = {"metadata": {"name": "mybackup", "namespace": "default"}}

    stale_pvc = MagicMock()
    stale_pvc.metadata.name = "k8si-snap-mybackup-20260101000000"

    with (
        patch("k8si.operator.workflow.quiesce.quiesce_context") as mock_ctx,
        patch("k8si.operator.workflow.snapshot.create_snapshot", new_callable=AsyncMock),
        patch("k8si.operator.workflow.snapshot.create_pvc_from_snapshot", new_callable=AsyncMock),
        patch("k8si.operator.workflow.snapshot.delete_snapshot_and_pvc", new_callable=AsyncMock),
        patch("k8si.operator.workflow._find_pvc_node_sync", return_value=None),
        patch("k8si.operator.workflow._run_job", new_callable=AsyncMock, return_value=""),
        patch(
            "k8si.operator.workflow._cleanup_orphan_snap_pvcs", new_callable=AsyncMock
        ) as mock_cleanup,
        patch("k8si.operator.workflow.kopf.event"),
    ):
        mock_ctx.return_value = MagicMock()
        asyncio.run(run_backup("mybackup", "default", spec, MagicMock(), body))
        mock_cleanup.assert_called_once_with("mybackup", "default")


# ── spec.checkAfterBackup ─────────────────────────────────────────────────────


def test_check_after_backup_injects_run_check_env() -> None:
    """When spec.checkAfterBackup is True, RUN_CHECK=true must appear in the job env."""
    from k8si.operator.workflow import _build_backup_job

    spec = {"checkAfterBackup": True}
    job = _build_backup_job("job-1", "default", "pvc-1", "secret-1", spec, [], {}, None)
    env_map = {
        e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env_map.get("RUN_CHECK") == "true"


def test_check_after_backup_absent_when_not_set() -> None:
    """Without checkAfterBackup in spec, RUN_CHECK must not appear in job env."""
    from k8si.operator.workflow import _build_backup_job

    spec = {}
    job = _build_backup_job("job-1", "default", "pvc-1", "secret-1", spec, [], {}, None)
    env_names = [e["name"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]]
    assert "RUN_CHECK" not in env_names


# ── _emit_event ───────────────────────────────────────────────────────────────


def test_emit_event_body_none_does_not_call_kopf() -> None:
    from k8si.operator.workflow import _emit_event

    with patch("k8si.operator.workflow.kopf.event") as mock_event:
        _emit_event(None, "Normal", "TestReason", "msg")
    mock_event.assert_not_called()


def test_emit_event_swallows_kopf_exception() -> None:
    from k8si.operator.workflow import _emit_event

    with patch("k8si.operator.workflow.kopf.event", side_effect=Exception("kopf down")):
        _emit_event({"metadata": {}}, "Normal", "TestReason", "msg")  # must not raise


# ── _cleanup_orphan_snap_pvcs ─────────────────────────────────────────────────


def test_cleanup_orphan_snap_pvcs_deletes_stale_pvcs() -> None:
    from k8si.operator.workflow import _cleanup_orphan_snap_pvcs

    stale = MagicMock()
    stale.metadata.name = "k8si-snap-mybackup-20260601000000"
    unrelated = MagicMock()
    unrelated.metadata.name = "myapp-data"
    mock_v1 = MagicMock()
    mock_v1.list_namespaced_persistent_volume_claim.return_value.items = [stale, unrelated]

    with patch("k8si.operator.workflow.kubernetes.client.CoreV1Api", return_value=mock_v1):
        asyncio.run(_cleanup_orphan_snap_pvcs("mybackup", "default"))

    mock_v1.delete_namespaced_persistent_volume_claim.assert_called_once_with(
        "k8si-snap-mybackup-20260601000000", "default"
    )


def test_cleanup_orphan_delete_failure_is_logged_not_raised() -> None:
    from k8si.operator.workflow import _cleanup_orphan_snap_pvcs

    stale = MagicMock()
    stale.metadata.name = "k8si-snap-x-20260601000000"
    mock_v1 = MagicMock()
    mock_v1.list_namespaced_persistent_volume_claim.return_value.items = [stale]
    mock_v1.delete_namespaced_persistent_volume_claim.side_effect = Exception("api error")

    with patch("k8si.operator.workflow.kubernetes.client.CoreV1Api", return_value=mock_v1):
        asyncio.run(_cleanup_orphan_snap_pvcs("x", "default"))  # must not raise


def test_cleanup_orphan_snap_pvcs_does_not_match_prefix_collision() -> None:
    """k8si-snap-app-<ts> must not match a cleanup for backup 'app-db' (and vice versa).

    A plain string prefix check (`name.startswith(f"k8si-snap-{name}-")`) is
    ambiguous: cleaning up orphans for backup "app" would also match
    "k8si-snap-app-db-<ts>", which belongs to a different backup named "app-db"
    and may be actively in use by a concurrently running backup Job.
    """
    from k8si.operator.workflow import _cleanup_orphan_snap_pvcs

    own_orphan = MagicMock()
    own_orphan.metadata.name = "k8si-snap-app-20260601000000"
    other_backup_pvc = MagicMock()
    other_backup_pvc.metadata.name = "k8si-snap-app-db-20260601000000"
    mock_v1 = MagicMock()
    mock_v1.list_namespaced_persistent_volume_claim.return_value.items = [
        own_orphan,
        other_backup_pvc,
    ]

    with patch("k8si.operator.workflow.kubernetes.client.CoreV1Api", return_value=mock_v1):
        asyncio.run(_cleanup_orphan_snap_pvcs("app", "default"))

    mock_v1.delete_namespaced_persistent_volume_claim.assert_called_once_with(
        "k8si-snap-app-20260601000000", "default"
    )


# ── _find_pvc_node_sync ───────────────────────────────────────────────────────


def test_find_pvc_node_sync_returns_node() -> None:
    from k8si.operator.workflow import _find_pvc_node_sync

    vol = MagicMock()
    vol.persistent_volume_claim.claim_name = "my-pvc"
    pod = MagicMock()
    pod.spec.volumes = [vol]
    pod.spec.node_name = "worker-1"
    mock_v1 = MagicMock()
    mock_v1.list_namespaced_pod.return_value.items = [pod]

    with patch("k8si.operator.workflow.kubernetes.client.CoreV1Api", return_value=mock_v1):
        result = _find_pvc_node_sync("my-pvc", "default")

    assert result == "worker-1"


def test_find_pvc_node_sync_no_match_returns_none() -> None:
    from k8si.operator.workflow import _find_pvc_node_sync

    mock_v1 = MagicMock()
    mock_v1.list_namespaced_pod.return_value.items = []

    with patch("k8si.operator.workflow.kubernetes.client.CoreV1Api", return_value=mock_v1):
        result = _find_pvc_node_sync("my-pvc", "default")

    assert result is None


def _mounting_pod(pvc: str, node: str, phase: str) -> MagicMock:
    vol = MagicMock()
    vol.persistent_volume_claim.claim_name = pvc
    pod = MagicMock()
    pod.spec.volumes = [vol]
    pod.spec.node_name = node
    pod.status.phase = phase
    return pod


def test_find_pvc_node_sync_skips_terminal_pods() -> None:
    """A dead pod (Succeeded/Failed) still carries its node assignment for a
    while and still lists the PVC in spec.volumes — pinning the backup Job to
    that node attaches it where the volume no longer lives. Only non-terminal
    pods may elect the node."""
    from k8si.operator.workflow import _find_pvc_node_sync

    pods = [
        _mounting_pod("my-pvc", "dead-node-a", "Succeeded"),
        _mounting_pod("my-pvc", "dead-node-b", "Failed"),
        _mounting_pod("my-pvc", "live-node", "Running"),
    ]
    mock_v1 = MagicMock()
    mock_v1.list_namespaced_pod.return_value.items = pods

    with patch("k8si.operator.workflow.kubernetes.client.CoreV1Api", return_value=mock_v1):
        result = _find_pvc_node_sync("my-pvc", "default")

    assert result == "live-node"


def test_find_pvc_node_sync_all_terminal_returns_none() -> None:
    """When every pod mounting the PVC is terminal there is no live placement:
    the Job must float (None), not pin to a dead pod's node."""
    from k8si.operator.workflow import _find_pvc_node_sync

    pods = [_mounting_pod("my-pvc", "dead-node", "Succeeded")]
    mock_v1 = MagicMock()
    mock_v1.list_namespaced_pod.return_value.items = pods

    with patch("k8si.operator.workflow.kubernetes.client.CoreV1Api", return_value=mock_v1):
        result = _find_pvc_node_sync("my-pvc", "default")

    assert result is None


# ── _build_backup_job: tags ───────────────────────────────────────────────────


def test_build_backup_job_includes_tags() -> None:
    from k8si.operator.workflow import _build_backup_job

    job = _build_backup_job("job-1", "default", "pvc-1", "secret-1", {}, ["app=test"], {}, None)
    env_map = {
        e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env_map.get("BACKUP_TAGS") == "app=test"


# ── _build_backup_job: BACKEND_TYPE propagation ──────────────────────────────


def test_build_backup_job_injects_backend_type() -> None:
    """BACKEND_TYPE must appear in the Job container env so kopia/restic uses correct backend."""
    import k8si.operator.workflow as wf
    from k8si.operator.workflow import _build_backup_job

    original = wf.BACKEND_TYPE
    wf.BACKEND_TYPE = "kopia"
    try:
        job = _build_backup_job("job-1", "default", "pvc-1", "secret-1", {}, [], {}, None)
    finally:
        wf.BACKEND_TYPE = original

    env_map = {
        e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env_map.get("BACKEND_TYPE") == "kopia"


def test_build_backup_job_backend_type_defaults_to_restic() -> None:
    """When BACKEND_TYPE is restic, env must still be injected with value restic."""
    import k8si.operator.workflow as wf
    from k8si.operator.workflow import _build_backup_job

    original = wf.BACKEND_TYPE
    wf.BACKEND_TYPE = "restic"
    try:
        job = _build_backup_job("job-1", "default", "pvc-1", "secret-1", {}, [], {}, None)
    finally:
        wf.BACKEND_TYPE = original

    env_map = {
        e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env_map.get("BACKEND_TYPE") == "restic"


# ── backup secret selection by backend type ───────────────────────────────────


def test_resolve_backup_secret_uses_kopia_secret_for_kopia() -> None:
    """When backend_type=kopia, _resolve_backup_secret uses kopiaSecret from spec."""
    from k8si.operator.workflow import _resolve_backup_secret

    spec = {"kopiaSecret": "my-kopia-secret", "resticSecret": "my-restic-secret"}
    assert _resolve_backup_secret(spec, "kopia") == "my-kopia-secret"


def test_resolve_backup_secret_falls_back_to_restic_secret_if_no_kopia_secret() -> None:
    """kopia backend without kopiaSecret falls back to resticSecret."""
    from k8si.operator.workflow import _resolve_backup_secret

    spec = {"resticSecret": "shared-secret"}
    assert _resolve_backup_secret(spec, "kopia") == "shared-secret"


def test_resolve_backup_secret_uses_restic_secret_for_restic() -> None:
    """restic backend uses resticSecret."""
    from k8si.operator.workflow import _resolve_backup_secret

    spec = {"resticSecret": "my-restic-secret", "kopiaSecret": "other-secret"}
    assert _resolve_backup_secret(spec, "restic") == "my-restic-secret"


# ── _get_pod_failure_reason: additional paths ─────────────────────────────────


def test_get_pod_failure_reason_uses_last_state() -> None:
    from k8si.operator.workflow import _get_pod_failure_reason

    cs = MagicMock()
    cs.state.terminated = None
    cs.last_state.terminated.reason = "OOMKilled"
    cs.last_state.terminated.exit_code = 137
    pod = MagicMock()
    pod.status.container_statuses = [cs]
    mock_v1 = MagicMock()
    mock_v1.list_namespaced_pod.return_value.items = [pod]

    reason = _get_pod_failure_reason(mock_v1, "test-job", "default")
    assert "OOMKill" in reason


def test_get_pod_failure_reason_term_none_returns_default() -> None:
    from k8si.operator.workflow import _get_pod_failure_reason

    cs = MagicMock()
    cs.state.terminated = None
    cs.last_state.terminated = None
    pod = MagicMock()
    pod.status.container_statuses = [cs]
    mock_v1 = MagicMock()
    mock_v1.list_namespaced_pod.return_value.items = [pod]

    reason = _get_pod_failure_reason(mock_v1, "test-job", "default")
    assert reason == "non-zero exit code"


def test_get_pod_failure_reason_exception_returns_default() -> None:
    from k8si.operator.workflow import _get_pod_failure_reason

    mock_v1 = MagicMock()
    mock_v1.list_namespaced_pod.side_effect = Exception("api down")

    reason = _get_pod_failure_reason(mock_v1, "test-job", "default")
    assert reason == "non-zero exit code"


# ── _wait_job_complete_sync: success & timeout paths ─────────────────────────


def test_wait_job_complete_sync_succeeds_immediately() -> None:
    job = MagicMock()
    job.status.succeeded = 1
    job.status.failed = 0
    mock_batch = MagicMock()
    mock_batch.read_namespaced_job.return_value = job

    with (
        patch("k8si.operator.workflow.kubernetes.client.BatchV1Api", return_value=mock_batch),
        patch("k8si.operator.workflow.kubernetes.client.CoreV1Api"),
    ):
        _wait_job_complete_sync("test-job", "default", 60)  # must not raise


def test_wait_job_complete_sync_pending_then_succeeds() -> None:
    pending = MagicMock()
    pending.status.succeeded = 0
    pending.status.failed = 0
    success = MagicMock()
    success.status.succeeded = 1
    success.status.failed = 0
    mock_batch = MagicMock()
    mock_batch.read_namespaced_job.side_effect = [pending, success]

    with (
        patch("k8si.operator.workflow.kubernetes.client.BatchV1Api", return_value=mock_batch),
        patch("k8si.operator.workflow.kubernetes.client.CoreV1Api"),
        patch("k8si.operator.workflow.time.sleep"),
    ):
        _wait_job_complete_sync("test-job", "default", 60)


def test_wait_job_complete_sync_times_out() -> None:
    pending = MagicMock()
    pending.status.succeeded = 0
    pending.status.failed = 0
    mock_batch = MagicMock()
    mock_batch.read_namespaced_job.return_value = pending

    with (
        patch("k8si.operator.workflow.kubernetes.client.BatchV1Api", return_value=mock_batch),
        patch("k8si.operator.workflow.kubernetes.client.CoreV1Api"),
        patch("k8si.operator.workflow.time.sleep"),
        patch("k8si.operator.workflow.time.monotonic", side_effect=[0.0, 0.0, 9999.0]),
    ):
        with pytest.raises(TimeoutError, match="timed out"):
            _wait_job_complete_sync("test-job", "default", 60)


# ── _collect_job_logs: empty / exception paths ────────────────────────────────


def test_collect_job_logs_no_pods_returns_empty() -> None:
    from k8si.operator.workflow import _collect_job_logs

    mock_v1 = MagicMock()
    mock_v1.list_namespaced_pod.return_value.items = []
    assert _collect_job_logs(mock_v1, "test-job", "default") == ""


def test_collect_job_logs_exception_returns_empty() -> None:
    from k8si.operator.workflow import _collect_job_logs

    mock_v1 = MagicMock()
    mock_v1.list_namespaced_pod.side_effect = Exception("api error")
    assert _collect_job_logs(mock_v1, "test-job", "default") == ""


# ── _wait_job_gone_sync ───────────────────────────────────────────────────────


def test_wait_job_gone_sync_returns_on_404() -> None:
    from k8si.operator.workflow import _wait_job_gone_sync

    exc = kubernetes.client.exceptions.ApiException(status=404)
    exc.status = 404
    mock_batch = MagicMock()
    mock_batch.read_namespaced_job.side_effect = exc

    with patch("k8si.operator.workflow.kubernetes.client.BatchV1Api", return_value=mock_batch):
        _wait_job_gone_sync("test-job", "default")  # must not raise


def test_wait_job_gone_sync_reraises_non_404() -> None:
    from k8si.operator.workflow import _wait_job_gone_sync

    exc = kubernetes.client.exceptions.ApiException(status=500)
    exc.status = 500
    mock_batch = MagicMock()
    mock_batch.read_namespaced_job.side_effect = exc

    with patch("k8si.operator.workflow.kubernetes.client.BatchV1Api", return_value=mock_batch):
        with pytest.raises(kubernetes.client.exceptions.ApiException):
            _wait_job_gone_sync("test-job", "default")


def test_wait_job_gone_sync_times_out_without_raising() -> None:
    from k8si.operator.workflow import _wait_job_gone_sync

    mock_batch = MagicMock()
    mock_batch.read_namespaced_job.return_value = MagicMock()

    with (
        patch("k8si.operator.workflow.kubernetes.client.BatchV1Api", return_value=mock_batch),
        patch("k8si.operator.workflow.time.sleep"),
        patch(
            "k8si.operator.workflow.time.monotonic",
            side_effect=[0.0, 0.0, 9999.0, 9999.0, 9999.0],
        ),
    ):
        _wait_job_gone_sync("test-job", "default")  # must not raise


# ── _run_job: full pipeline ───────────────────────────────────────────────────


def test_run_job_creates_waits_and_deletes() -> None:
    from k8si.operator.workflow import _run_job

    job_body = {"metadata": {"name": "test-job"}, "spec": {}}
    mock_batch = MagicMock()
    mock_v1 = MagicMock()
    mock_v1.list_namespaced_pod.return_value.items = []

    with (
        patch("k8si.operator.workflow.kubernetes.client.BatchV1Api", return_value=mock_batch),
        patch("k8si.operator.workflow.kubernetes.client.CoreV1Api", return_value=mock_v1),
        patch("k8si.operator.workflow._wait_job_complete_sync"),
        patch("k8si.operator.workflow._wait_job_gone_sync"),
    ):
        asyncio.run(_run_job(job_body, "default", 60, logging.getLogger("test")))

    mock_batch.create_namespaced_job.assert_called_once_with("default", job_body)
    mock_batch.delete_namespaced_job.assert_called_once()


def test_run_job_returns_logs_before_pod_deletion() -> None:
    """_run_job must collect logs BEFORE deleting pods; returning them to the caller."""
    from k8si.operator.workflow import _run_job

    job_body = {"metadata": {"name": "test-job"}, "spec": {}}
    mock_batch = MagicMock()
    mock_v1 = MagicMock()
    mock_pod = MagicMock()
    mock_pod.metadata.name = "test-job-abc"
    mock_v1.list_namespaced_pod.return_value.items = [mock_pod]
    mock_v1.read_namespaced_pod_log.return_value = "snapshot abc12345 saved"

    call_order: list[str] = []
    mock_v1.read_namespaced_pod_log.side_effect = lambda *a, **kw: (
        call_order.append("logs_read") or "snapshot abc12345 saved"
    )
    mock_batch.delete_namespaced_job.side_effect = lambda *a, **kw: call_order.append("deleted")

    with (
        patch("k8si.operator.workflow.kubernetes.client.BatchV1Api", return_value=mock_batch),
        patch("k8si.operator.workflow.kubernetes.client.CoreV1Api", return_value=mock_v1),
        patch("k8si.operator.workflow._wait_job_complete_sync"),
        patch("k8si.operator.workflow._wait_job_gone_sync"),
    ):
        result = asyncio.run(_run_job(job_body, "default", 60, logging.getLogger("test")))

    assert result == "snapshot abc12345 saved"
    assert call_order == ["logs_read", "deleted"], "logs must be read before pods are deleted"


def test_run_job_deletes_even_on_failure() -> None:
    from k8si.operator.workflow import _run_job

    job_body = {"metadata": {"name": "test-job"}, "spec": {}}
    mock_batch = MagicMock()
    mock_v1 = MagicMock()
    mock_v1.list_namespaced_pod.return_value.items = []

    with (
        patch("k8si.operator.workflow.kubernetes.client.BatchV1Api", return_value=mock_batch),
        patch("k8si.operator.workflow.kubernetes.client.CoreV1Api", return_value=mock_v1),
        patch(
            "k8si.operator.workflow._wait_job_complete_sync",
            side_effect=RuntimeError("job failed"),
        ),
        patch("k8si.operator.workflow._wait_job_gone_sync"),
    ):
        with pytest.raises(RuntimeError):
            asyncio.run(_run_job(job_body, "default", 60, logging.getLogger("test")))

    mock_batch.delete_namespaced_job.assert_called_once()


# ── run_backup: exception paths ───────────────────────────────────────────────


def test_run_backup_snapshot_phase_exception_emits_warning() -> None:
    spec = {"pvc": "test-pvc", "resticSecret": "test-secret"}
    body = {"metadata": {}}

    with (
        patch("k8si.operator.workflow._cleanup_orphan_snap_pvcs", new_callable=AsyncMock),
        patch("k8si.operator.workflow.quiesce.quiesce_context") as mock_ctx,
        patch(
            "k8si.operator.workflow.snapshot.create_snapshot",
            new_callable=AsyncMock,
            side_effect=RuntimeError("snap failed"),
        ),
        patch("k8si.operator.workflow.snapshot.delete_snapshot_and_pvc", new_callable=AsyncMock),
        patch("k8si.operator.workflow._find_pvc_node_sync", return_value=None),
        patch("k8si.operator.workflow.kopf.event") as mock_event,
    ):
        mock_ctx.return_value = MagicMock()
        with pytest.raises(RuntimeError, match="snap failed"):
            asyncio.run(run_backup("test", "default", spec, MagicMock(), body))

    reasons = [call[1]["reason"] for call in mock_event.call_args_list]
    assert "SnapshotFailed" in reasons


def test_run_backup_phase2_exception_cleans_up_snapshot() -> None:
    """When the backup job fails, snapshot and PVC are still deleted (finally block)."""
    spec = {"pvc": "test-pvc", "resticSecret": "test-secret"}
    body = {"metadata": {}}

    with (
        patch("k8si.operator.workflow._cleanup_orphan_snap_pvcs", new_callable=AsyncMock),
        patch("k8si.operator.workflow.quiesce.quiesce_context") as mock_ctx,
        patch("k8si.operator.workflow.snapshot.create_snapshot", new_callable=AsyncMock),
        patch("k8si.operator.workflow.snapshot.create_pvc_from_snapshot", new_callable=AsyncMock),
        patch(
            "k8si.operator.workflow.snapshot.delete_snapshot_and_pvc", new_callable=AsyncMock
        ) as mock_delete,
        patch("k8si.operator.workflow._find_pvc_node_sync", return_value=None),
        patch(
            "k8si.operator.workflow._run_job",
            new_callable=AsyncMock,
            side_effect=RuntimeError("backup failed"),
        ),
        patch("k8si.operator.workflow.kopf.event"),
    ):
        mock_ctx.return_value = MagicMock()
        with pytest.raises(RuntimeError, match="backup failed"):
            asyncio.run(run_backup("test", "default", spec, MagicMock(), body))

    mock_delete.assert_called_once()


def test_run_backup_direct_mode_exception_emits_warning() -> None:
    spec = {"pvc": "test-pvc", "resticSecret": "test-secret", "backupMode": "direct"}
    body = {"metadata": {}}

    with (
        patch("k8si.operator.workflow._cleanup_orphan_snap_pvcs", new_callable=AsyncMock),
        patch("k8si.operator.workflow.quiesce.quiesce_context") as mock_ctx,
        patch("k8si.operator.workflow._find_pvc_node_sync", return_value=None),
        patch(
            "k8si.operator.workflow._run_job",
            new_callable=AsyncMock,
            side_effect=RuntimeError("direct failed"),
        ),
        patch("k8si.operator.workflow.kopf.event") as mock_event,
    ):
        mock_ctx.return_value = MagicMock()
        with pytest.raises(RuntimeError, match="direct failed"):
            asyncio.run(run_backup("test", "default", spec, MagicMock(), body))

    reasons = [call[1]["reason"] for call in mock_event.call_args_list]
    assert "BackupFailed" in reasons


# ── _run_hook_job ─────────────────────────────────────────────────────────────


def test_run_hook_job_optional_failure_does_not_raise() -> None:
    from k8si.operator.workflow import _run_hook_job

    with (
        patch("k8si.operator.workflow._find_pvc_node_sync", return_value=None),
        patch(
            "k8si.operator.workflow._run_job",
            new_callable=AsyncMock,
            side_effect=RuntimeError("hook failed"),
        ),
    ):
        asyncio.run(
            _run_hook_job("/usr/local/bin/hook.sh", False, "default", "pvc", logging.getLogger("t"))
        )


def test_run_hook_job_required_failure_raises() -> None:
    from k8si.operator.workflow import _run_hook_job

    with (
        patch("k8si.operator.workflow._find_pvc_node_sync", return_value=None),
        patch(
            "k8si.operator.workflow._run_job",
            new_callable=AsyncMock,
            side_effect=RuntimeError("hook failed"),
        ),
    ):
        with pytest.raises(RuntimeError, match="Pre-snapshot hook"):
            asyncio.run(
                _run_hook_job(
                    "/usr/local/bin/hook.sh", True, "default", "pvc", logging.getLogger("t")
                )
            )


def test_run_hook_job_pins_to_node_when_found() -> None:
    """When _find_pvc_node_sync returns a node, hook job gets nodeSelector (lines 162, 185)."""
    from k8si.operator.workflow import _run_hook_job

    with (
        patch("k8si.operator.workflow._find_pvc_node_sync", return_value="worker-1"),
        patch(
            "k8si.operator.workflow._run_job", new_callable=AsyncMock, return_value=""
        ) as mock_run,
    ):
        asyncio.run(_run_hook_job("/hook.sh", False, "default", "pvc", logging.getLogger("t")))

    job_body = mock_run.call_args[0][0]
    assert job_body["spec"]["template"]["spec"]["nodeSelector"] == {
        "kubernetes.io/hostname": "worker-1"
    }


def test_run_backup_direct_mode_with_db_and_hook_emits_events() -> None:
    """Direct mode with db_spec + preSnapshotHook emits QuiesceStarted and HookStarted."""
    spec = {
        "pvc": "test-pvc",
        "resticSecret": "test-secret",
        "backupMode": "direct",
        "database": {"type": "mariadb", "secretRef": "db-secret"},
        "preSnapshotHook": "/usr/local/bin/hook.sh",
    }
    body = {"metadata": {}}

    with (
        patch("k8si.operator.workflow._cleanup_orphan_snap_pvcs", new_callable=AsyncMock),
        patch("k8si.operator.workflow.quiesce.quiesce_context") as mock_ctx,
        patch("k8si.operator.workflow._find_pvc_node_sync", return_value=None),
        patch("k8si.operator.workflow._run_hook_job", new_callable=AsyncMock),
        patch("k8si.operator.workflow._run_job", new_callable=AsyncMock, return_value=""),
        patch("k8si.operator.workflow.kopf.event") as mock_event,
    ):
        mock_ctx.return_value = MagicMock()
        asyncio.run(run_backup("test", "default", spec, MagicMock(), body))

    reasons = [call[1]["reason"] for call in mock_event.call_args_list]
    assert "QuiesceStarted" in reasons
    assert "HookStarted" in reasons


def test_run_job_cleanup_exception_is_swallowed() -> None:
    """delete_namespaced_job failure in _run_job finally is silently ignored."""
    from k8si.operator.workflow import _run_job

    job_body = {"metadata": {"name": "test-job"}, "spec": {}}
    mock_batch = MagicMock()
    mock_batch.delete_namespaced_job.side_effect = Exception("api error")

    with (
        patch("k8si.operator.workflow.kubernetes.client.BatchV1Api", return_value=mock_batch),
        patch("k8si.operator.workflow._wait_job_complete_sync"),
        patch("k8si.operator.workflow._wait_job_gone_sync"),
    ):
        asyncio.run(_run_job(job_body, "default", 60, logging.getLogger("test")))  # must not raise


# ── _write_run_log ─────────────────────────────────────────────────────────────


def test_write_run_log_patches_crd() -> None:
    from k8si.operator.workflow import _write_run_log

    entries = [{"time": "2026-06-12T10:00:00Z", "phase": "BackupJobStarted", "message": "starting"}]
    with patch("k8si.operator.workflow.kubernetes.client.CustomObjectsApi") as mock_cls:
        _write_run_log("myapp", "default", entries)

    call = mock_cls.return_value.patch_namespaced_custom_object_status.call_args
    assert call.kwargs["name"] == "myapp"
    assert call.kwargs["namespace"] == "default"
    assert call.kwargs["body"]["status"]["lastRunLog"] == entries


def test_write_run_log_swallows_api_exception() -> None:
    from kubernetes.client.exceptions import ApiException

    from k8si.operator.workflow import _write_run_log

    with patch("k8si.operator.workflow.kubernetes.client.CustomObjectsApi") as mock_cls:
        mock_cls.return_value.patch_namespaced_custom_object_status.side_effect = ApiException(
            status=403
        )
        _write_run_log("myapp", "default", [])  # must not raise


def test_run_backup_clears_log_at_start() -> None:
    """run_backup writes an empty lastRunLog list before any phase entry."""
    import copy

    calls: list[dict] = []

    def capture(*args, **kwargs):  # noqa: ANN001,ANN002,ANN003
        calls.append(copy.deepcopy(kwargs.get("body", {})))

    spec = {"pvc": "pvc", "resticSecret": "sec", "schedule": "0 2 * * *"}

    with (
        patch("k8si.operator.workflow._cleanup_orphan_snap_pvcs", new_callable=AsyncMock),
        patch("k8si.operator.workflow.quiesce.quiesce_context") as mock_ctx,
        patch("k8si.operator.workflow.snapshot.create_snapshot", new_callable=AsyncMock),
        patch("k8si.operator.workflow.snapshot.create_pvc_from_snapshot", new_callable=AsyncMock),
        patch("k8si.operator.workflow.snapshot.delete_snapshot_and_pvc", new_callable=AsyncMock),
        patch("k8si.operator.workflow._find_pvc_node_sync", return_value=None),
        patch("k8si.operator.workflow._run_job", new_callable=AsyncMock, return_value=""),
        patch("k8si.operator.workflow.kopf.event"),
        patch("k8si.operator.workflow.kubernetes.client.CustomObjectsApi") as mock_api_cls,
    ):
        mock_ctx.return_value = MagicMock()
        mock_api_cls.return_value.patch_namespaced_custom_object_status.side_effect = capture
        asyncio.run(run_backup("myapp", "default", spec, MagicMock()))

    assert calls, "Expected at least one PATCH call"
    first = calls[0]
    assert first.get("status", {}).get("lastRunLog") == [], "First call must clear the log"


def test_run_backup_logs_backup_job_started_phase() -> None:
    """BackupJobStarted must appear in lastRunLog written to CRD during run_backup."""
    logged_phases: list[str] = []

    def capture(*args, **kwargs):  # noqa: ANN001,ANN002,ANN003
        for entry in kwargs.get("body", {}).get("status", {}).get("lastRunLog", []):
            logged_phases.append(entry["phase"])

    spec = {"pvc": "pvc", "resticSecret": "sec", "schedule": "0 2 * * *"}

    with (
        patch("k8si.operator.workflow._cleanup_orphan_snap_pvcs", new_callable=AsyncMock),
        patch("k8si.operator.workflow.quiesce.quiesce_context") as mock_ctx,
        patch("k8si.operator.workflow.snapshot.create_snapshot", new_callable=AsyncMock),
        patch("k8si.operator.workflow.snapshot.create_pvc_from_snapshot", new_callable=AsyncMock),
        patch("k8si.operator.workflow.snapshot.delete_snapshot_and_pvc", new_callable=AsyncMock),
        patch("k8si.operator.workflow._find_pvc_node_sync", return_value=None),
        patch("k8si.operator.workflow._run_job", new_callable=AsyncMock, return_value=""),
        patch("k8si.operator.workflow.kopf.event"),
        patch("k8si.operator.workflow.kubernetes.client.CustomObjectsApi") as mock_api_cls,
    ):
        mock_ctx.return_value = MagicMock()
        mock_api_cls.return_value.patch_namespaced_custom_object_status.side_effect = capture
        asyncio.run(run_backup("myapp", "default", spec, MagicMock()))

    assert "BackupJobStarted" in logged_phases, f"Expected BackupJobStarted, got {logged_phases}"
    assert "BackupJobCompleted" in logged_phases


def test_log_phase_offloads_status_patch_via_to_thread() -> None:
    """_log_phase (and the initial log-clear) must call _write_run_log/_patch_run_status
    via asyncio.to_thread, never directly on the event loop — a direct call blocks Kopf's
    asyncio loop (and every other backup's timers/reconciliation) for the full k8s API
    round-trip, and _log_phase runs ~6x per backup run."""
    from k8si.operator import workflow

    real_to_thread = asyncio.to_thread
    to_thread_funcs: list[object] = []

    async def spy_to_thread(func, *args, **kwargs):  # noqa: ANN001,ANN002,ANN003
        to_thread_funcs.append(func)
        return await real_to_thread(func, *args, **kwargs)

    spec = {"pvc": "pvc", "resticSecret": "sec", "schedule": "0 2 * * *"}

    with (
        patch("k8si.operator.workflow._cleanup_orphan_snap_pvcs", new_callable=AsyncMock),
        patch("k8si.operator.workflow.quiesce.quiesce_context") as mock_ctx,
        patch("k8si.operator.workflow.snapshot.create_snapshot", new_callable=AsyncMock),
        patch("k8si.operator.workflow.snapshot.create_pvc_from_snapshot", new_callable=AsyncMock),
        patch("k8si.operator.workflow.snapshot.delete_snapshot_and_pvc", new_callable=AsyncMock),
        patch("k8si.operator.workflow._find_pvc_node_sync", return_value=None),
        patch("k8si.operator.workflow._run_job", new_callable=AsyncMock, return_value=""),
        patch("k8si.operator.workflow.kopf.event"),
        patch("k8si.operator.workflow.kubernetes.client.CustomObjectsApi"),
        patch("k8si.operator.workflow.asyncio.to_thread", side_effect=spy_to_thread),
    ):
        mock_ctx.return_value = MagicMock()
        asyncio.run(run_backup("myapp", "default", spec, MagicMock()))

    # Initial log-clear + SnapshotStarted/Created + BackupJobStarted/Completed = 5 patches,
    # every single one must be routed through asyncio.to_thread.
    assert workflow._write_run_log in to_thread_funcs, (
        "_write_run_log must be invoked via asyncio.to_thread, not directly on the event loop"
    )
    assert to_thread_funcs.count(workflow._write_run_log) >= 5


# ── goals #4/#5/#6: snapshot cleanup, jobName, bounded concurrency ──────────


def test_run_backup_returns_job_name():
    """#5: the reconciler looks Jobs up by the RUN name, but Jobs are named
    k8si-{backup}-{ts} with a timestamp minted inside run_backup — the names
    can never match. run_backup must return the actual Job name so the caller
    can record it on the run status."""
    spec = {
        "pvc": "test-pvc",
        "resticSecret": "test-secret",
        "schedule": "0 2 * * *",
        "volumeSnapshotClass": "test-snapclass",
    }
    body = {"metadata": {"name": "test-backup", "namespace": "default"}}
    _snap = "k8si.operator.workflow.snapshot"

    with (
        patch("k8si.operator.workflow._cleanup_orphan_snap_pvcs", new_callable=AsyncMock),
        patch("k8si.operator.workflow.quiesce.quiesce_context"),
        patch(f"{_snap}.create_snapshot", new_callable=AsyncMock),
        patch(f"{_snap}.create_pvc_from_snapshot", new_callable=AsyncMock),
        patch(f"{_snap}.delete_snapshot_and_pvc", new_callable=AsyncMock),
        patch("k8si.operator.workflow._find_pvc_node_sync", return_value="node1"),
        patch("k8si.operator.workflow._run_job", new_callable=AsyncMock) as mock_run_job,
    ):
        mock_run_job.return_value = "Created snapshot with root k1 and ID abc in 1s"
        result = asyncio.run(run_backup("test-backup", "default", spec, MagicMock(), body))

    job_name = mock_run_job.call_args[0][0]["metadata"]["name"]
    assert job_name.startswith("k8si-test-backup-"), job_name
    assert result["jobName"] == job_name


def test_snapshot_phase_failure_cleans_up_created_snapshot():
    """#4: a VolumeSnapshot left behind by a failed phase 1 wedges every later
    run for 30 minutes inside the snapshot-conflict wait — the snapshot must
    be deleted before the failure is re-raised."""
    spec = {
        "pvc": "test-pvc",
        "resticSecret": "test-secret",
        "schedule": "0 2 * * *",
        "volumeSnapshotClass": "test-snapclass",
    }
    body = {"metadata": {"name": "test-backup", "namespace": "default"}}
    _snap = "k8si.operator.workflow.snapshot"

    with (
        patch("k8si.operator.workflow._cleanup_orphan_snap_pvcs", new_callable=AsyncMock),
        patch("k8si.operator.workflow.quiesce.quiesce_context"),
        patch(f"{_snap}.create_snapshot", new_callable=AsyncMock) as mock_create_snap,
        patch(f"{_snap}.delete_snapshot_and_pvc", new_callable=AsyncMock) as mock_delete,
    ):
        mock_create_snap.side_effect = TimeoutError("snapshot not ready after 300s")
        with pytest.raises(TimeoutError):
            asyncio.run(run_backup("test-backup", "default", spec, MagicMock(), body))

    mock_delete.assert_called_once()
    assert mock_delete.call_args[0][0] == "default"
    assert mock_delete.call_args[0][1].startswith("k8si-test-backup-")
    assert mock_delete.call_args[0][2] is None  # no ephemeral PVC exists yet


def test_cleanup_orphan_snap_pvcs_also_sweeps_volume_snapshots():
    """#4: the orphan sweep must cover leftover k8si-{name}-<ts> VolumeSnapshots
    too, not just the ephemeral PVCs — both wedge later runs."""
    with (
        patch("kubernetes.client.CoreV1Api") as mock_v1_cls,
        patch("kubernetes.client.CustomObjectsApi") as mock_custom_cls,
    ):
        v1 = MagicMock()
        v1.list_namespaced_persistent_volume_claim.return_value = MagicMock(items=[])
        mock_v1_cls.return_value = v1
        custom = MagicMock()
        custom.list_namespaced_custom_object.return_value = {
            "items": [
                {"metadata": {"name": "k8si-test-backup-20260101000000"}},
                {"metadata": {"name": "k8si-other-backup-20260101000000"}},
                {"metadata": {"name": "someone-elses-snapshot"}},
            ]
        }
        mock_custom_cls.return_value = custom

        asyncio.run(_cleanup_orphan_snap_pvcs("test-backup", "default"))

    custom.delete_namespaced_custom_object.assert_called_once_with(
        "snapshot.storage.k8s.io",
        "v1",
        "default",
        "volumesnapshots",
        "k8si-test-backup-20260101000000",
    )


def test_run_backup_concurrency_is_capped():
    """#6: concurrent run_backup executions must be bounded — each parks a
    worker for the full job duration, and unbounded concurrency starves the
    shared default executor until every timer (including backup_timer) freezes
    (the recorded scheduler-hang bug)."""
    import k8si.operator.workflow as wf

    spec = {
        "pvc": "test-pvc",
        "resticSecret": "test-secret",
        "schedule": "0 2 * * *",
        "backupMode": "direct",
    }
    body = {"metadata": {"name": "test-backup", "namespace": "default"}}
    _snap = "k8si.operator.workflow.snapshot"
    state = {"live": 0, "max": 0}

    async def probe_job(job_body, namespace, timeout, logger):
        state["live"] += 1
        state["max"] = max(state["max"], state["live"])
        await asyncio.sleep(0.05)
        state["live"] -= 1
        return "Created snapshot with root k1 and ID abc in 1s"

    async def run_all():
        with (
            patch("k8si.operator.workflow._cleanup_orphan_snap_pvcs", new_callable=AsyncMock),
            patch("k8si.operator.workflow.quiesce.quiesce_context"),
            patch("k8si.operator.workflow._find_pvc_node_sync", return_value=None),
            patch("k8si.operator.workflow._run_job", probe_job),
        ):
            return await asyncio.gather(
                *[
                    wf.run_backup("test-backup", "default", spec, MagicMock(), body)
                    for _ in range(4)
                ]
            )

    asyncio.run(run_all())
    assert state["max"] <= wf._MAX_CONCURRENT_BACKUPS, (
        f"observed {state['max']} concurrent run_backups, cap is {wf._MAX_CONCURRENT_BACKUPS}"
    )


def test_cleanup_skips_pvcs_mounted_by_running_pods():
    """#8: after an operator restart, a re-invoked run_backup's orphan sweep
    could delete the ephemeral PVC that the ORIGINAL still-running backup Job
    has mounted. PVCs mounted by any pod in the namespace must be skipped."""
    orphan = MagicMock()
    orphan.metadata.name = "k8si-snap-x-20260101000000"
    mounted = MagicMock()
    mounted.metadata.name = "k8si-snap-x-20260202000000"
    mock_v1 = MagicMock()
    mock_v1.list_namespaced_persistent_volume_claim.return_value.items = [orphan, mounted]

    pod = MagicMock()
    vol = MagicMock()
    vol.persistent_volume_claim.claim_name = "k8si-snap-x-20260202000000"
    vol.persistent_volume_claim = vol.persistent_volume_claim  # keep attribute
    other = MagicMock()
    other.persistent_volume_claim = None
    pod.spec.volumes = [vol, other]
    mock_v1.list_namespaced_pod.return_value.items = [pod]

    with (
        patch("kubernetes.client.CoreV1Api", return_value=mock_v1),
        patch("kubernetes.client.CustomObjectsApi") as mock_custom_cls,
    ):
        custom = MagicMock()
        custom.list_namespaced_custom_object.return_value = {"items": []}
        mock_custom_cls.return_value = custom

        asyncio.run(_cleanup_orphan_snap_pvcs("x", "default"))

    mock_v1.delete_namespaced_persistent_volume_claim.assert_called_once_with(
        "k8si-snap-x-20260101000000", "default"
    )
