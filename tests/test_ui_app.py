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
    "resticSecret": None,
    "kopiaSecret": None,
    "backupSecret": None,
    "lastBackupTime": "2024-01-15T02:00:00Z",
    "lastBackupResult": "success",
    "nextBackupTime": "2024-01-16T02:00:00Z",
    "triggeredAt": None,
    "lastRunRef": None,
    "message": "Backup completed",
    "recentBackups": [
        {"time": "2024-01-15T02:00:00Z", "result": "success"},
    ],
    "recentRuns": [],
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


def test_shape_includes_recent_runs() -> None:
    """_shape() must expose recentRuns so the dashboard can populate the run-picker and teeth."""
    from k8si.ui.app import _shape

    item = {
        "metadata": {"name": "bkp", "namespace": "default"},
        "spec": {"pvc": "data", "schedule": "0 2 * * *"},
        "status": {
            "recentRuns": [
                {"name": "bkp-20260617120000", "time": "2026-06-17T12:00:00Z", "result": "success"},
                {"name": "bkp-20260616120000", "time": "2026-06-16T12:00:00Z", "result": "failed"},
            ]
        },
    }

    result = _shape(item)

    assert "recentRuns" in result
    assert len(result["recentRuns"]) == 2
    assert result["recentRuns"][0]["name"] == "bkp-20260617120000"
    assert result["recentRuns"][0]["result"] == "success"


def test_shape_recent_runs_defaults_to_empty() -> None:
    """_shape() returns empty recentRuns when the field is absent from status."""
    from k8si.ui.app import _shape

    item = {
        "metadata": {"name": "bkp", "namespace": "default"},
        "spec": {"pvc": "data", "schedule": ""},
        "status": {},
    }

    result = _shape(item)
    assert result["recentRuns"] == []


def test_shape_includes_restic_secret() -> None:
    """_shape() exposes spec.resticSecret so the dashboard can show the destination."""
    from k8si.ui.app import _shape

    item = {
        "metadata": {"name": "bkp", "namespace": "default"},
        "spec": {"pvc": "data", "schedule": "", "resticSecret": "my-restic-secret"},
        "status": {},
    }

    result = _shape(item)
    assert result.get("resticSecret") == "my-restic-secret"
    assert result.get("backupSecret") == "my-restic-secret"


def test_shape_restic_secret_defaults_to_none() -> None:
    """_shape() returns None for resticSecret when absent from spec."""
    from k8si.ui.app import _shape

    result = _shape(MINIMAL_ITEM)
    assert result.get("resticSecret") is None
    assert result.get("backupSecret") is None


def test_shape_includes_kopia_secret() -> None:
    """_shape() exposes spec.kopiaSecret; backupSecret prefers kopiaSecret over resticSecret."""
    from k8si.ui.app import _shape

    item = {
        "metadata": {"name": "bkp", "namespace": "default"},
        "spec": {
            "pvc": "data",
            "schedule": "",
            "kopiaSecret": "my-kopia-secret",
            "resticSecret": "my-restic-secret",
        },
        "status": {},
    }

    result = _shape(item)
    assert result.get("kopiaSecret") == "my-kopia-secret"
    assert result.get("backupSecret") == "my-kopia-secret"


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


# ---------------------------------------------------------------------------
def _logs_client() -> TestClient:
    return make_ui_client(raise_server_exceptions=False)


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

    not_found = importlib.metadata.PackageNotFoundError
    with patch.dict(os.environ, {"K8SI_VERSION": "0.8.0rc8-test"}):
        with patch.object(importlib.metadata, "version", side_effect=not_found):
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


def test_legacy_backup_logs_endpoint_removed() -> None:
    """The legacy SSE endpoint (/api/backups/{ns}/{name}/logs) read
    status.lastRunLog — a field nothing has written since the 0.10.0 cleanup.
    Dead endpoints get deleted, not maintained (slop rule)."""
    client = make_ui_client(raise_server_exceptions=False)
    paths = {getattr(r, "path", "") for r in client.app.routes}
    assert "/api/backups/{namespace}/{name}/logs" not in paths, (
        "the legacy per-backup logs route must not be registered at all"
    )


def test_static_assets_served() -> None:
    """The dashboard shell loads its CSS/JS from /static (app mount) — a broken
    mount would serve a bare unstyled shell."""
    client = make_ui_client()
    css = client.get("/static/app.css")
    js = client.get("/static/app.js")
    assert css.status_code == 200 and ".backup-table" in css.text
    assert js.status_code == 200 and "function render" in js.text
