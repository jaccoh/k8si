"""Tests for POST /api/backups/{namespace}/{name}/trigger in k8si/ui/app.py."""

from unittest.mock import MagicMock, patch

import kubernetes.client.exceptions
from fastapi.testclient import TestClient


def _make_client() -> TestClient:
    """Return a TestClient with K8s startup disabled."""
    from k8si.ui import app as app_module

    app_module.app.router.on_startup.clear()
    from k8si.ui.app import app

    return TestClient(app)


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_trigger_returns_200_and_patches_status(mock_api_cls: MagicMock) -> None:
    """POST /api/backups/{ns}/{name}/trigger patches status.triggeredAt and returns 200."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.return_value = {"metadata": {}}

    client = _make_client()
    resp = client.post("/api/backups/default/mybackup/trigger")

    assert resp.status_code == 200
    data = resp.json()
    assert data["triggered"] is True
    assert "triggeredAt" in data

    mock_api.patch_namespaced_custom_object_status.assert_called_once()
    call_args = mock_api.patch_namespaced_custom_object_status.call_args
    # Positional: (group, version, namespace, plural, name, body)
    body = call_args.args[5] if len(call_args.args) > 5 else call_args.kwargs.get("body", {})
    assert "triggeredAt" in body.get("status", {})


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_trigger_returns_404_for_missing_backup(mock_api_cls: MagicMock) -> None:
    """POST /api/backups/{ns}/{name}/trigger returns 404 when the backup does not exist."""
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
    """trigger endpoint passes namespace and name through to the k8s PATCH call."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.return_value = {"metadata": {}}

    client = _make_client()
    client.post("/api/backups/production/my-db-backup/trigger")

    get_call = mock_api.get_namespaced_custom_object.call_args
    assert get_call.args[2] == "production"
    assert get_call.args[4] == "my-db-backup"

    patch_call = mock_api.patch_namespaced_custom_object_status.call_args
    assert patch_call.args[2] == "production"
    assert patch_call.args[4] == "my-db-backup"
