"""Tests for the operator startup health check in k8si/operator/main.py."""

import logging
from unittest.mock import MagicMock, patch

import kubernetes.client.exceptions


def _api_exc(status: int) -> kubernetes.client.exceptions.ApiException:
    e = kubernetes.client.exceptions.ApiException(status=status)
    return e


# ── CRD accessible ────────────────────────────────────────────────────────────


def test_health_check_passes_when_crd_accessible(caplog):
    from k8si.operator.main import _check_prerequisites

    mock_api = MagicMock()
    mock_api.list_cluster_custom_object.return_value = {"items": []}
    with patch("k8si.operator.main.kubernetes.client.CustomObjectsApi", return_value=mock_api):
        with caplog.at_level(logging.ERROR, logger="k8si"):
            _check_prerequisites(logging.getLogger("k8si"))

    assert not any("HEALTH" in r.message and r.levelno >= logging.ERROR for r in caplog.records)


# ── CRD missing (404) ─────────────────────────────────────────────────────────


def test_health_check_logs_error_when_crd_missing(caplog):
    from k8si.operator.main import _check_prerequisites

    mock_api = MagicMock()
    mock_api.list_cluster_custom_object.side_effect = _api_exc(404)
    with patch("k8si.operator.main.kubernetes.client.CustomObjectsApi", return_value=mock_api):
        with caplog.at_level(logging.ERROR, logger="k8si"):
            _check_prerequisites(logging.getLogger("k8si"))

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "expected an ERROR log"
    assert "crd_run.yaml" in errors[0].message.lower() or "crd" in errors[0].message.lower()


# ── RBAC denied (403) ────────────────────────────────────────────────────────


def test_health_check_logs_error_when_rbac_denied(caplog):
    from k8si.operator.main import _check_prerequisites

    mock_api = MagicMock()
    mock_api.list_cluster_custom_object.side_effect = _api_exc(403)
    with patch("k8si.operator.main.kubernetes.client.CustomObjectsApi", return_value=mock_api):
        with caplog.at_level(logging.ERROR, logger="k8si"):
            _check_prerequisites(logging.getLogger("k8si"))

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "expected an ERROR log"
    assert "rbac" in errors[0].message.lower()


# ── other error: warning, not crash ──────────────────────────────────────────


def test_health_check_logs_warning_on_unknown_error(caplog):
    from k8si.operator.main import _check_prerequisites

    mock_api = MagicMock()
    mock_api.list_cluster_custom_object.side_effect = RuntimeError("network timeout")
    with patch("k8si.operator.main.kubernetes.client.CustomObjectsApi", return_value=mock_api):
        with caplog.at_level(logging.WARNING, logger="k8si"):
            _check_prerequisites(logging.getLogger("k8si"))

    assert any("HEALTH" in r.message for r in caplog.records)
