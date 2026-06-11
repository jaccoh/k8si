"""Tests for k8si/operator/workflow.py and Kopf event logging."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from k8si.operator.workflow import _wait_job_complete_sync, run_backup


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
        patch("k8si.operator.workflow._run_job", new_callable=AsyncMock),
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
        patch("k8si.operator.workflow._run_job", new_callable=AsyncMock) as mock_run_job,
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
        patch("k8si.operator.workflow._run_job", new_callable=AsyncMock),
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
        e["name"]: e.get("value")
        for e in job["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env_map.get("RUN_CHECK") == "true"


def test_check_after_backup_absent_when_not_set() -> None:
    """Without checkAfterBackup in spec, RUN_CHECK must not appear in job env."""
    from k8si.operator.workflow import _build_backup_job

    spec = {}
    job = _build_backup_job("job-1", "default", "pvc-1", "secret-1", spec, [], {}, None)
    env_names = [e["name"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]]
    assert "RUN_CHECK" not in env_names
