"""Tests for POST /api/backups/{namespace}/{name}/trigger in k8si/ui/app.py."""

from unittest.mock import MagicMock, patch

import kubernetes.client.exceptions
from fastapi.testclient import TestClient

from tests.helpers import make_ui_client


def _make_client() -> TestClient:
    return make_ui_client()


def _backup_obj(uid: str = "test-uid") -> dict:
    return {"metadata": {"uid": uid}, "spec": {"backupMode": "snapshot"}}


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_trigger_creates_run_and_returns_run_name(mock_api_cls: MagicMock) -> None:
    """POST trigger creates a K8siBackupRun and returns triggered=True with runName."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.return_value = _backup_obj()

    client = _make_client()
    resp = client.post("/api/backups/default/mybackup/trigger")

    assert resp.status_code == 200
    data = resp.json()
    assert data["triggered"] is True
    assert "triggeredAt" in data
    assert "runName" in data
    assert data["runName"].startswith("mybackup-")

    mock_api.create_namespaced_custom_object.assert_called_once()
    call_args = mock_api.create_namespaced_custom_object.call_args
    run_obj = call_args.args[4] if len(call_args.args) > 4 else call_args.kwargs.get("body", {})
    assert run_obj["spec"]["backupRef"] == "mybackup"
    assert run_obj["spec"]["triggeredBy"] == "manual"


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_trigger_returns_404_for_missing_backup(mock_api_cls: MagicMock) -> None:
    """POST trigger returns 404 when the backup does not exist."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.side_effect = kubernetes.client.exceptions.ApiException(
        status=404
    )

    client = _make_client()
    resp = client.post("/api/backups/default/nonexistent/trigger")

    assert resp.status_code == 404


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_trigger_uses_correct_namespace_and_name(mock_api_cls: MagicMock) -> None:
    """Trigger passes namespace and name through to the K8siBackupRun create call."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.return_value = _backup_obj()

    client = _make_client()
    client.post("/api/backups/production/my-db-backup/trigger")

    get_call = mock_api.get_namespaced_custom_object.call_args
    assert get_call.args[2] == "production"
    assert get_call.args[4] == "my-db-backup"

    create_call = mock_api.create_namespaced_custom_object.call_args
    assert create_call.args[2] == "production"
    run_obj = create_call.args[4]
    assert run_obj["spec"]["backupRef"] == "my-db-backup"


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_trigger_returns_500_on_get_api_error(mock_api_cls: MagicMock) -> None:
    """POST trigger returns 500 on non-404 ApiException from get."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.side_effect = kubernetes.client.exceptions.ApiException(
        status=403
    )

    client = _make_client()
    resp = client.post("/api/backups/default/mybackup/trigger")

    assert resp.status_code == 500


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_trigger_returns_500_on_create_error(mock_api_cls: MagicMock) -> None:
    """POST trigger returns 500 when creating the K8siBackupRun fails."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.return_value = _backup_obj()
    mock_api.list_namespaced_custom_object.return_value = {"items": []}
    mock_api.create_namespaced_custom_object.side_effect = (
        kubernetes.client.exceptions.ApiException(status=500)
    )

    client = _make_client()
    resp = client.post("/api/backups/default/mybackup/trigger")

    assert resp.status_code == 500


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_trigger_returns_409_when_run_active(mock_api_cls: MagicMock) -> None:
    """POST trigger returns 409 when a K8siBackupRun is already Pending or Running."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.return_value = _backup_obj()
    mock_api.list_namespaced_custom_object.return_value = {
        "items": [
            {
                "metadata": {"name": "mybackup-20260614120000"},
                "status": {"phase": "Running"},
            }
        ]
    }

    client = _make_client()
    resp = client.post("/api/backups/default/mybackup/trigger")

    assert resp.status_code == 409
    assert "mybackup-20260614120000" in resp.json()["detail"]


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_trigger_sets_lastbackupresult_running_on_parent(mock_api_cls: MagicMock) -> None:
    """POST trigger must patch lastBackupResult=running on the parent K8siBackup immediately,
    so the counter tiles reflect the active state without waiting for operator reconciliation."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.return_value = _backup_obj()
    mock_api.list_namespaced_custom_object.return_value = {"items": []}

    client = _make_client()
    resp = client.post("/api/backups/default/mybackup/trigger")

    assert resp.status_code == 200
    run_name = resp.json()["runName"]

    mock_api.patch_namespaced_custom_object_status.assert_called_once()
    patch_body = mock_api.patch_namespaced_custom_object_status.call_args.args[5]
    assert patch_body["status"]["lastBackupResult"] == "running"
    assert patch_body["status"]["lastRunRef"] == run_name


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_trigger_allows_when_no_active_runs(mock_api_cls: MagicMock) -> None:
    """POST trigger succeeds when all existing runs are Succeeded or Failed."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.return_value = _backup_obj()
    mock_api.list_namespaced_custom_object.return_value = {
        "items": [
            {
                "metadata": {"name": "mybackup-20260613120000"},
                "status": {"phase": "Succeeded"},
            }
        ]
    }

    client = _make_client()
    resp = client.post("/api/backups/default/mybackup/trigger")

    assert resp.status_code == 200
