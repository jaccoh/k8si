"""Tests for k8si/operator/workflow.py and Kopf event logging."""

import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
import pytest

from k8si.operator.workflow import run_backup


def test_run_backup_emits_kopf_events() -> None:
    spec = {
        "pvc": "test-pvc",
        "resticSecret": "test-secret",
        "schedule": "0 2 * * *",
        "database": {"type": "postgres", "secretRef": "db-secret"},
        "preSnapshotHook": "/usr/local/lib/k8si/db-dump.sh",
    }
    body = {"metadata": {"name": "test-backup", "namespace": "default"}}

    # Mock all external dependencies of the workflow
    with patch("k8si.operator.workflow.quiesce.quiesce_context") as mock_quiesce_ctx, \
         patch("k8si.operator.workflow.snapshot.create_snapshot", new_callable=AsyncMock) as mock_create_snap, \
         patch("k8si.operator.workflow.snapshot.create_pvc_from_snapshot", new_callable=AsyncMock) as mock_create_pvc, \
         patch("k8si.operator.workflow.snapshot.delete_snapshot_and_pvc", new_callable=AsyncMock) as mock_delete_snap_pvc, \
         patch("k8si.operator.workflow._find_pvc_node_sync", return_value="node1"), \
         patch("k8si.operator.workflow._run_job", new_callable=AsyncMock) as mock_run_job, \
         patch("k8si.operator.workflow._run_hook_job", new_callable=AsyncMock) as mock_run_hook, \
         patch("k8si.operator.workflow.kopf.event") as mock_kopf_event:

        # Set up quiesce context manager mock
        mock_ctx = MagicMock()
        mock_quiesce_ctx.return_value = mock_ctx

        # Execute
        result = asyncio.run(run_backup("test-backup", "default", spec, MagicMock(), body))

        # Assertions
        assert result["lastBackupResult"] == "success"
        
        # Verify kopf.event was called at milestones:
        # 1. QuiesceStarted
        # 2. HookStarted
        # 3. SnapshotStarted
        # 4. SnapshotCreated
        # 5. BackupJobStarted
        # 6. BackupJobCompleted
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

    # Mock all external dependencies
    with patch("k8si.operator.workflow.quiesce.quiesce_context") as mock_quiesce_ctx, \
         patch("k8si.operator.workflow.snapshot.create_snapshot", new_callable=AsyncMock) as mock_create_snap, \
         patch("k8si.operator.workflow.snapshot.create_pvc_from_snapshot", new_callable=AsyncMock) as mock_create_pvc, \
         patch("k8si.operator.workflow.snapshot.delete_snapshot_and_pvc", new_callable=AsyncMock) as mock_delete_snap_pvc, \
         patch("k8si.operator.workflow._find_pvc_node_sync", return_value="node1"), \
         patch("k8si.operator.workflow._run_job", new_callable=AsyncMock) as mock_run_job, \
         patch("k8si.operator.workflow.kopf.event") as mock_kopf_event:

        mock_quiesce_ctx.return_value = MagicMock()

        # Execute
        result = asyncio.run(run_backup("test-backup", "default", spec, MagicMock(), body))

        # Assertions
        assert result["lastBackupResult"] == "success"
        
        # Snapshot creation must be bypassed completely
        mock_create_snap.assert_not_called()
        mock_create_pvc.assert_not_called()
        mock_delete_snap_pvc.assert_not_called()
        
        # Verify run_job was called with the live PVC (test-pvc)
        mock_run_job.assert_called_once()
        job_body = mock_run_job.call_args[0][0]
        pvc_spec = job_body["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]
        assert pvc_spec["claimName"] == "test-pvc"
