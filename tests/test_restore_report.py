"""Unit tests for _report_to_crd() in restore.py."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from k8si.config import Config
from k8si.restore import _report_to_crd


def _cfg(
    backup_name: str | None = "my-backup",
    backup_namespace: str | None = "default",
) -> Config:
    return Config(
        mode="restore",
        data_path=Path("/data"),
        restic_repository="sftp:fake",
        restic_password="secret",
        restic_password_file=None,
        backup_name=backup_name,
        backup_namespace=backup_namespace,
    )


def _result(result: str = "success", snapshot_id: str = "abc12345") -> dict[str, str]:
    return {"result": result, "snapshot_id": snapshot_id, "message": "Restored"}


def test_no_api_call_when_backup_name_absent() -> None:
    with patch("k8si.restore.kubernetes.client.CustomObjectsApi") as mock_cls:
        _report_to_crd(_cfg(backup_name=None), _result())
    mock_cls.assert_not_called()


def test_patches_status_subresource_not_root() -> None:
    with patch("k8si.restore.kubernetes.client.CustomObjectsApi") as mock_cls:
        _report_to_crd(_cfg(), _result())
    mock_api = mock_cls.return_value
    mock_api.patch_namespaced_custom_object_status.assert_called_once()
    mock_api.patch_namespaced_custom_object.assert_not_called()


def test_patch_targets_correct_crd() -> None:
    with patch("k8si.restore.kubernetes.client.CustomObjectsApi") as mock_cls:
        _report_to_crd(_cfg(), _result())
    call = mock_cls.return_value.patch_namespaced_custom_object_status.call_args
    assert call.kwargs["group"] == "k8si.io"
    assert call.kwargs["version"] == "v1"
    assert call.kwargs["plural"] == "k8sibackups"
    assert call.kwargs["name"] == "my-backup"
    assert call.kwargs["namespace"] == "default"


def test_patch_body_contains_all_status_fields() -> None:
    with patch("k8si.restore.kubernetes.client.CustomObjectsApi") as mock_cls:
        _report_to_crd(_cfg(), {"result": "success", "snapshot_id": "abc12345", "message": "Restored from abc12345"})
    body = mock_cls.return_value.patch_namespaced_custom_object_status.call_args.kwargs["body"]
    status = body["status"]
    assert status["lastRestoreResult"] == "success"
    assert status["lastRestoreSnapshotId"] == "abc12345"
    assert status["lastRestoreMessage"] == "Restored from abc12345"
    assert "lastRestoreTime" in status


def test_patch_failure_is_best_effort_no_exception() -> None:
    from kubernetes.client.exceptions import ApiException

    with patch("k8si.restore.kubernetes.client.CustomObjectsApi") as mock_cls:
        mock_cls.return_value.patch_namespaced_custom_object_status.side_effect = ApiException(status=403)
        _report_to_crd(_cfg(), _result())  # must not raise
