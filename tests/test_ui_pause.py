"""Tests for PATCH /api/backups/{namespace}/{name}/paused in k8si/ui/app.py."""

from unittest.mock import MagicMock, patch

import kubernetes.client.exceptions
from fastapi.testclient import TestClient

from tests.helpers import make_ui_client


def _make_client() -> TestClient:
    return make_ui_client()


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_pause_sets_spec_paused_true(mock_api_cls: MagicMock) -> None:
    """PATCH /api/backups/{ns}/{name}/paused with paused=true patches spec.paused=true."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.return_value = {"metadata": {}}

    client = _make_client()
    resp = client.patch("/api/backups/default/mybackup/paused", json={"paused": True})

    assert resp.status_code == 200
    assert resp.json()["paused"] is True

    mock_api.patch_namespaced_custom_object.assert_called_once()
    call_args = mock_api.patch_namespaced_custom_object.call_args
    body = call_args.args[5] if len(call_args.args) > 5 else call_args.kwargs.get("body", {})
    assert body["spec"]["paused"] is True


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_resume_sets_spec_paused_false(mock_api_cls: MagicMock) -> None:
    """PATCH /api/backups/{ns}/{name}/paused with paused=false patches spec.paused=false."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.return_value = {"metadata": {}}

    client = _make_client()
    resp = client.patch("/api/backups/default/mybackup/paused", json={"paused": False})

    assert resp.status_code == 200
    assert resp.json()["paused"] is False

    call_args = mock_api.patch_namespaced_custom_object.call_args
    body = call_args.args[5] if len(call_args.args) > 5 else call_args.kwargs.get("body", {})
    assert body["spec"]["paused"] is False


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_pause_returns_404_for_missing_backup(mock_api_cls: MagicMock) -> None:
    """PATCH /api/backups/{ns}/{name}/paused returns 404 when the backup does not exist."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.side_effect = kubernetes.client.exceptions.ApiException(
        status=404
    )

    client = _make_client()
    resp = client.patch("/api/backups/default/nonexistent/paused", json={"paused": True})

    assert resp.status_code == 404


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_pause_uses_correct_namespace_and_name(mock_api_cls: MagicMock) -> None:
    """PATCH endpoint passes namespace and name through to the k8s PATCH call."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.return_value = {"metadata": {}}

    client = _make_client()
    client.patch("/api/backups/production/my-db-backup/paused", json={"paused": True})

    patch_call = mock_api.patch_namespaced_custom_object.call_args
    assert patch_call.args[2] == "production"
    assert patch_call.args[4] == "my-db-backup"


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_pause_returns_500_on_get_api_error(mock_api_cls: MagicMock) -> None:
    """PATCH /api/backups/{ns}/{name}/paused returns 500 on non-404 ApiException from get."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.side_effect = kubernetes.client.exceptions.ApiException(
        status=403
    )

    client = _make_client()
    resp = client.patch("/api/backups/default/mybackup/paused", json={"paused": True})

    assert resp.status_code == 500


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_pause_returns_500_on_patch_api_error(mock_api_cls: MagicMock) -> None:
    """PATCH /api/backups/{ns}/{name}/paused returns 500 when the spec PATCH call fails."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.return_value = {"metadata": {}}
    mock_api.patch_namespaced_custom_object.side_effect = kubernetes.client.exceptions.ApiException(
        status=500
    )

    client = _make_client()
    resp = client.patch("/api/backups/default/mybackup/paused", json={"paused": True})

    assert resp.status_code == 500
