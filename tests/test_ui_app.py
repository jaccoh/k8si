"""Tests for k8si web UI FastAPI app — written FIRST (TDD)."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.helpers import make_ui_client

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
    "lastRunRef": None,
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
    return make_ui_client()


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


def test_healthz_returns_ok() -> None:
    """GET /healthz returns {"status": "ok"} with 200."""
    client = _make_client()
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_index_returns_html() -> None:
    """GET / returns the dashboard HTML file with 200."""
    client = _make_client()
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# _load_k8s() unit tests (covers lines 20-23)
# ---------------------------------------------------------------------------


@patch("kubernetes.config.load_incluster_config")
def test_load_k8s_uses_incluster_config(mock_incluster: MagicMock) -> None:
    """_load_k8s() calls load_incluster_config() when it succeeds."""
    from k8si.ui.app import _load_k8s

    _load_k8s()
    mock_incluster.assert_called_once()


@patch("kubernetes.config.load_incluster_config")
@patch("kubernetes.config.load_kube_config")
def test_load_k8s_falls_back_to_kube_config(
    mock_kube: MagicMock, mock_incluster: MagicMock
) -> None:
    """_load_k8s() falls back to load_kube_config when incluster raises."""
    import kubernetes.config

    mock_incluster.side_effect = kubernetes.config.ConfigException("not in cluster")

    from k8si.ui.app import _load_k8s

    _load_k8s()
    mock_kube.assert_called_once()


# ---------------------------------------------------------------------------
# lifespan() unit test (covers lines 28-29)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GET /api/backups/{ns}/{name}/logs — SSE streaming endpoint
# ---------------------------------------------------------------------------


def _logs_client() -> TestClient:
    return make_ui_client(raise_server_exceptions=False)


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
@patch("k8si.ui.app.kubernetes.client.CoreV1Api")
def test_logs_endpoint_streams_phase_entries(mock_core: MagicMock, mock_custom: MagicMock) -> None:
    """GET /logs streams phase entries from lastRunLog and closes on success."""
    mock_obj = {
        "status": {
            "lastBackupResult": "success",
            "lastRunLog": [
                {"time": "2026-06-12T10:00:00Z", "phase": "BackupJobStarted", "message": "start"}
            ],
        }
    }
    mock_custom.return_value.get_namespaced_custom_object.return_value = mock_obj

    client = _logs_client()
    resp = client.get("/api/backups/default/myapp/logs")

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    assert "BackupJobStarted" in body
    assert '"type": "phase"' in body
    assert '"type": "done"' in body


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_logs_endpoint_404_on_missing_backup(mock_custom: MagicMock) -> None:
    """GET /logs returns 404 when the CRD does not exist."""
    import kubernetes.client.exceptions

    exc = kubernetes.client.exceptions.ApiException(status=404)
    exc.status = 404
    mock_custom.return_value.get_namespaced_custom_object.side_effect = exc

    client = _logs_client()
    resp = client.get("/api/backups/default/missing/logs")

    assert resp.status_code == 404


def test_is_new_run_none_since() -> None:
    """since=None → always treat as new run."""
    from k8si.ui.app import _is_new_run

    assert _is_new_run("2026-01-01T00:00:00Z", None) is True


def test_is_new_run_same_timestamp() -> None:
    """since == lastBackupTime → not a new run (stale)."""
    from k8si.ui.app import _is_new_run

    assert _is_new_run("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z") is False


def test_is_new_run_newer_timestamp() -> None:
    """lastBackupTime > since → new run completed."""
    from k8si.ui.app import _is_new_run

    assert _is_new_run("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z") is True


def test_is_new_run_missing_last_time() -> None:
    """No lastBackupTime → not a new run yet."""
    from k8si.ui.app import _is_new_run

    assert _is_new_run(None, "2026-01-01T00:00:00Z") is False


@patch("k8si.ui.app.asyncio.sleep")
@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
@patch("k8si.ui.app.kubernetes.client.CoreV1Api")
def test_logs_endpoint_since_new_run_emits_done(
    mock_core: MagicMock, mock_custom: MagicMock, mock_sleep: MagicMock
) -> None:
    """GET /logs?since=T emits done when lastBackupTime > T (new run completed)."""
    mock_sleep.return_value = None
    stale = {
        "status": {
            "lastBackupResult": "success",
            "lastBackupTime": "2026-01-01T00:00:00Z",
            "lastRunLog": [],
        }
    }
    new_run = {
        "status": {
            "lastBackupResult": "success",
            "lastBackupTime": "2026-01-02T00:00:00Z",
            "lastRunLog": [
                {"time": "2026-01-02T00:00:00Z", "phase": "BackupJobStarted", "message": "go"}
            ],
        }
    }
    mock_custom.return_value.get_namespaced_custom_object.side_effect = [stale, new_run]

    client = _logs_client()
    resp = client.get("/api/backups/default/myapp/logs?since=2026-01-01T00:00:00Z")

    assert resp.status_code == 200
    assert "BackupJobStarted" in resp.text
    assert '"result": "success"' in resp.text


def test_is_new_run_type_error_caught() -> None:
    """Mixed aware/naive datetimes raise TypeError — must be caught, not propagate."""
    from k8si.ui.app import _is_new_run

    # naive since, aware last_backup_time → TypeError without the fix
    assert _is_new_run("2026-01-02T00:00:00+00:00", "2026-01-01T00:00:00") is True


@patch("k8si.ui.app.asyncio.sleep")
@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
@patch("k8si.ui.app.kubernetes.client.CoreV1Api")
def test_logs_filters_stale_entries(
    mock_core: MagicMock, mock_custom: MagicMock, mock_sleep: MagicMock
) -> None:
    """Entries with time <= since are not emitted; entries after since are emitted."""
    mock_sleep.return_value = None
    mock_obj = {
        "status": {
            "lastBackupResult": "success",
            "lastBackupTime": "2026-01-02T00:00:00Z",
            "lastRunLog": [
                {"time": "2026-01-01T00:00:00Z", "phase": "OldPhase", "message": "stale"},
                {"time": "2026-01-02T01:00:00Z", "phase": "NewPhase", "message": "fresh"},
            ],
        }
    }
    mock_custom.return_value.get_namespaced_custom_object.return_value = mock_obj

    client = _logs_client()
    resp = client.get("/api/backups/default/myapp/logs?since=2026-01-01T12:00:00Z")

    assert resp.status_code == 200
    assert "OldPhase" not in resp.text
    assert "NewPhase" in resp.text


@patch("k8si.ui.app.asyncio.sleep")
@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
@patch("k8si.ui.app.kubernetes.client.CoreV1Api")
def test_logs_seen_resets_on_log_clear(
    mock_core: MagicMock, mock_custom: MagicMock, mock_sleep: MagicMock
) -> None:
    """When operator clears lastRunLog, seen resets so new entries are not skipped."""
    mock_sleep.return_value = None
    old_run = {
        "status": {
            "lastBackupResult": "success",
            "lastBackupTime": "2026-01-01T00:00:00Z",
            "lastRunLog": [
                {"time": "2026-01-01T00:00:00Z", "phase": "OldPhase", "message": "old"},
                {"time": "2026-01-01T00:00:01Z", "phase": "OldPhase2", "message": "old2"},
                {"time": "2026-01-01T00:00:02Z", "phase": "OldPhase3", "message": "old3"},
            ],
        }
    }
    cleared = {"status": {"lastBackupResult": "running", "lastRunLog": []}}
    new_run = {
        "status": {
            "lastBackupResult": "success",
            "lastBackupTime": "2026-01-02T00:00:00Z",
            "lastRunLog": [
                {"time": "2026-01-02T00:00:01Z", "phase": "NewPhase", "message": "new"},
            ],
        }
    }
    mock_custom.return_value.get_namespaced_custom_object.side_effect = [old_run, cleared, new_run]

    client = _logs_client()
    # since = old lastBackupTime, so old entries are filtered; new entry should appear
    resp = client.get("/api/backups/default/myapp/logs?since=2026-01-01T00:00:00Z")

    assert resp.status_code == 200
    assert "NewPhase" in resp.text


# ---------------------------------------------------------------------------
# GET /api/runs/{ns}/{runName}/logs — run-specific SSE endpoint
# ---------------------------------------------------------------------------


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_run_logs_streams_phase_entries(mock_custom: MagicMock) -> None:
    """GET /api/runs logs streams phase entries and closes on Succeeded."""
    run_obj_succeeded = {
        "status": {
            "phase": "Succeeded",
            "log": [{"time": "2026-06-14T10:00:00Z", "phase": "Snapshot", "message": "done"}],
        }
    }
    mock_custom.return_value.get_namespaced_custom_object.return_value = run_obj_succeeded

    client = _logs_client()
    resp = client.get("/api/runs/default/mybackup-20260614/logs")

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    assert "Snapshot" in body
    assert '"type": "phase"' in body
    assert '"type": "done"' in body
    assert '"result": "success"' in body


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_run_logs_emits_done_failed_on_failed_phase(mock_custom: MagicMock) -> None:
    """GET /api/runs logs emits done with result=failed when phase=Failed."""
    run_obj_failed = {"status": {"phase": "Failed", "log": [], "message": "timeout"}}
    mock_custom.return_value.get_namespaced_custom_object.return_value = run_obj_failed

    client = _logs_client()
    resp = client.get("/api/runs/default/mybackup-20260614/logs")

    assert resp.status_code == 200
    assert '"result": "failed"' in resp.text


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_run_logs_404_on_missing_run(mock_custom: MagicMock) -> None:
    """GET /api/runs logs returns 404 when the run does not exist."""
    import kubernetes.client.exceptions

    exc = kubernetes.client.exceptions.ApiException(status=404)
    exc.status = 404
    mock_custom.return_value.get_namespaced_custom_object.side_effect = exc

    client = _logs_client()
    resp = client.get("/api/runs/default/missing-run/logs")

    assert resp.status_code == 404


def test_version_endpoint_returns_version() -> None:
    """GET /api/version returns a non-empty version string."""
    client = _make_client()
    resp = client.get("/api/version")
    assert resp.status_code == 200
    data = resp.json()
    assert "version" in data
    assert isinstance(data["version"], str)
    assert len(data["version"]) > 0


def test_version_endpoint_prefers_k8si_version_env_var() -> None:
    """GET /api/version must return the K8SI_VERSION env var when set.

    The UI container copies app.py directly (no pip install), so importlib.metadata
    cannot resolve the package version. K8SI_VERSION is injected at image build time
    via --build-arg and must take precedence over any package lookup.
    """
    import importlib
    import os

    with patch.dict(os.environ, {"K8SI_VERSION": "0.8.0rc8-test"}):
        # Re-importing would cache — call the endpoint logic via the test client instead
        from k8si.ui import app as ui_app

        with patch.object(importlib.metadata, "version", side_effect=importlib.metadata.PackageNotFoundError):
            client = _make_client()
            resp = client.get("/api/version")

    assert resp.status_code == 200
    assert resp.json()["version"] == "0.8.0rc8-test"


@pytest.mark.anyio
async def test_lifespan_calls_load_k8s() -> None:
    """lifespan() calls _load_k8s() on startup and yields."""
    from k8si.ui import app as app_module

    with patch("k8si.ui.app._load_k8s") as mock_load:
        async with app_module.lifespan(app_module.app):
            pass

    mock_load.assert_called_once()
