"""Tests for k8si web UI FastAPI app — written FIRST (TDD)."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

FULL_ITEM = {
    "metadata": {
        "name": "my-backup",
        "namespace": "default",
    },
    "spec": {
        "pvc": "data-pvc",
        "schedule": "0 2 * * *",
        "backupWindow": {"start": "02:00", "end": "06:00"},
    },
    "status": {
        "lastBackupTime": "2024-01-15T02:00:00Z",
        "lastBackupResult": "success",
        "nextBackupTime": "2024-01-16T02:00:00Z",
        "message": "Backup completed",
        "recentBackups": [
            {"time": "2024-01-15T02:00:00Z", "result": "success"},
        ],
        "lastRestoreResult": "success",
        "lastRestoreTime": "2024-01-14T10:00:00Z",
        "lastRestoreMessage": "Restored from abc1234",
    },
}

EXPECTED_SHAPED = {
    "name": "my-backup",
    "namespace": "default",
    "pvc": "data-pvc",
    "schedule": "0 2 * * *",
    "paused": False,
    "backupWindow": {"start": "02:00", "end": "06:00"},
    "lastBackupTime": "2024-01-15T02:00:00Z",
    "lastBackupResult": "success",
    "nextBackupTime": "2024-01-16T02:00:00Z",
    "triggeredAt": None,
    "message": "Backup completed",
    "recentBackups": [
        {"time": "2024-01-15T02:00:00Z", "result": "success"},
    ],
    "successRate": 1.0,
    "streak": 1,
    "lastBackupDuration": None,
    "lastRestoreResult": "success",
    "lastRestoreTime": "2024-01-14T10:00:00Z",
    "lastRestoreMessage": "Restored from abc1234",
}

MINIMAL_ITEM = {
    "metadata": {"name": "bare-backup", "namespace": "kube-system"},
    "spec": {"pvc": "bare-pvc", "schedule": ""},
    # no "status" key at all
}


# ---------------------------------------------------------------------------
# _shape() unit tests
# ---------------------------------------------------------------------------


def test_shape_extracts_all_fields() -> None:
    """_shape() maps a full raw K8siBackup dict to the expected JSON shape."""
    from k8si.ui.app import _shape

    result = _shape(FULL_ITEM)

    assert result == EXPECTED_SHAPED


def test_shape_defaults_missing_status() -> None:
    """_shape() supplies safe defaults when status is absent."""
    from k8si.ui.app import _shape

    result = _shape(MINIMAL_ITEM)

    assert result["name"] == "bare-backup"
    assert result["namespace"] == "kube-system"
    assert result["pvc"] == "bare-pvc"
    assert result["lastBackupTime"] is None
    assert result["lastBackupResult"] == "pending"
    assert result["nextBackupTime"] is None
    assert result["message"] == ""
    assert result["recentBackups"] == []
    assert result["paused"] is False
    assert result["triggeredAt"] is None
    assert result["backupWindow"] == {}
    assert result["lastRestoreResult"] is None
    assert result["lastRestoreTime"] is None
    assert result["lastRestoreMessage"] is None
    assert result["successRate"] is None
    assert result["streak"] == 0
    assert result["lastBackupDuration"] is None


# ---------------------------------------------------------------------------
# /api/backups endpoint tests (K8s client mocked)
# ---------------------------------------------------------------------------


def _make_client() -> TestClient:
    """Return a TestClient with K8s startup disabled."""
    from k8si.ui import app as app_module

    # Patch startup so it doesn't try to connect to a cluster
    app_module.app.router.on_startup.clear()
    from k8si.ui.app import app

    return TestClient(app)


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_list_backups_returns_shaped_items(mock_api_cls: MagicMock) -> None:
    """GET /api/backups returns a list of shaped dicts for each CRD item."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.list_cluster_custom_object.return_value = {"items": [FULL_ITEM, MINIMAL_ITEM]}

    client = _make_client()
    response = client.get("/api/backups")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert data[0]["name"] == "my-backup"
    assert data[0]["lastBackupResult"] == "success"
    assert data[1]["name"] == "bare-backup"
    assert data[1]["lastBackupResult"] == "pending"


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_list_backups_empty_cluster(mock_api_cls: MagicMock) -> None:
    """GET /api/backups returns [] when there are no CRD objects."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.list_cluster_custom_object.return_value = {"items": []}

    client = _make_client()
    response = client.get("/api/backups")

    assert response.status_code == 200
    assert response.json() == []
