"""Tests for k8si/operator/snapshot.py."""

import time
from unittest.mock import MagicMock, patch

import kubernetes.client.exceptions
import pytest

import k8si.operator.snapshot as snap_mod


def _make_snapshot_item(pvc: str, ready: bool) -> dict:
    return {
        "spec": {"source": {"persistentVolumeClaimName": pvc}},
        "status": {"readyToUse": ready},
    }


class TestWaitNoSnapshotInProgress:
    """_wait_no_snapshot_in_progress_sync waits until no unready snapshot targets the PVC."""

    def test_proceeds_immediately_when_no_snapshots(self):
        mock_api = MagicMock()
        mock_api.list_namespaced_custom_object.return_value = {"items": []}

        with patch("k8si.operator.snapshot.kubernetes.client.CustomObjectsApi", return_value=mock_api):
            snap_mod._wait_no_snapshot_in_progress_sync("my-pvc", "default")

        assert mock_api.list_namespaced_custom_object.call_count == 1

    def test_proceeds_immediately_when_only_ready_snapshots_for_pvc(self):
        mock_api = MagicMock()
        mock_api.list_namespaced_custom_object.return_value = {
            "items": [_make_snapshot_item("my-pvc", ready=True)]
        }

        with patch("k8si.operator.snapshot.kubernetes.client.CustomObjectsApi", return_value=mock_api):
            snap_mod._wait_no_snapshot_in_progress_sync("my-pvc", "default")

        assert mock_api.list_namespaced_custom_object.call_count == 1

    def test_ignores_in_progress_snapshots_for_other_pvcs(self):
        mock_api = MagicMock()
        mock_api.list_namespaced_custom_object.return_value = {
            "items": [_make_snapshot_item("other-pvc", ready=False)]
        }

        with patch("k8si.operator.snapshot.kubernetes.client.CustomObjectsApi", return_value=mock_api):
            snap_mod._wait_no_snapshot_in_progress_sync("my-pvc", "default")

        assert mock_api.list_namespaced_custom_object.call_count == 1

    def test_waits_until_conflict_clears(self):
        responses = [
            {"items": [_make_snapshot_item("my-pvc", ready=False)]},
            {"items": [_make_snapshot_item("my-pvc", ready=False)]},
            {"items": [_make_snapshot_item("my-pvc", ready=True)]},
        ]
        mock_api = MagicMock()
        mock_api.list_namespaced_custom_object.side_effect = responses

        with (
            patch("k8si.operator.snapshot.kubernetes.client.CustomObjectsApi", return_value=mock_api),
            patch("k8si.operator.snapshot.time.sleep"),
        ):
            snap_mod._wait_no_snapshot_in_progress_sync("my-pvc", "default")

        assert mock_api.list_namespaced_custom_object.call_count == 3

    def test_times_out_when_conflict_never_clears(self):
        mock_api = MagicMock()
        mock_api.list_namespaced_custom_object.return_value = {
            "items": [_make_snapshot_item("my-pvc", ready=False)]
        }

        with (
            patch("k8si.operator.snapshot.kubernetes.client.CustomObjectsApi", return_value=mock_api),
            patch("k8si.operator.snapshot.time.sleep"),
            patch("k8si.operator.snapshot.time.monotonic", side_effect=[0.0, 0.0, 9999.0]),
        ):
            with pytest.raises(TimeoutError, match="my-pvc"):
                snap_mod._wait_no_snapshot_in_progress_sync("my-pvc", "default")


class TestDeadCodeRemoved:
    def test_wait_pvc_bound_sync_removed(self):
        """_wait_pvc_bound_sync must not exist — it deadlocks on WaitForFirstConsumer volumes."""
        assert not hasattr(snap_mod, "_wait_pvc_bound_sync")


class TestWaitSnapshotReadySyncRetry:
    def _make_api_exception(self, status: int) -> kubernetes.client.exceptions.ApiException:
        exc = kubernetes.client.exceptions.ApiException(status=status)
        exc.status = status
        return exc

    def _make_ready_response(self) -> dict:
        return {"status": {"readyToUse": True}}

    def test_transient_5xx_is_retried_and_succeeds(self):
        """A single 503 during polling must be retried, not raised."""
        calls = [
            self._make_api_exception(503),
            self._make_ready_response(),
        ]

        def side_effect(*args, **kwargs):
            result = calls.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        mock_custom_api = MagicMock()
        mock_custom_api.get_namespaced_custom_object.side_effect = side_effect

        with (
            patch("k8si.operator.snapshot.kubernetes.client.CustomObjectsApi", return_value=mock_custom_api),
            patch("k8si.operator.snapshot.time.sleep"),
        ):
            # Must complete without raising
            snap_mod._wait_snapshot_ready_sync("test-snap", "default")

        assert mock_custom_api.get_namespaced_custom_object.call_count == 2

    def test_non_transient_4xx_is_not_retried_but_raised(self):
        """A 404 during polling must be re-raised immediately, not retried."""
        mock_custom_api = MagicMock()
        mock_custom_api.get_namespaced_custom_object.side_effect = self._make_api_exception(404)

        with (
            patch("k8si.operator.snapshot.kubernetes.client.CustomObjectsApi", return_value=mock_custom_api),
            patch("k8si.operator.snapshot.time.sleep"),
        ):
            with pytest.raises(kubernetes.client.exceptions.ApiException) as exc_info:
                snap_mod._wait_snapshot_ready_sync("test-snap", "default")

        assert exc_info.value.status == 404
        # Must not have retried — only one call before raising
        assert mock_custom_api.get_namespaced_custom_object.call_count == 1

    def test_multiple_5xx_then_success(self):
        """Multiple consecutive 5xx errors must all be retried until success."""
        calls = [
            self._make_api_exception(500),
            self._make_api_exception(503),
            self._make_api_exception(502),
            self._make_ready_response(),
        ]

        def side_effect(*args, **kwargs):
            result = calls.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        mock_custom_api = MagicMock()
        mock_custom_api.get_namespaced_custom_object.side_effect = side_effect

        with (
            patch("k8si.operator.snapshot.kubernetes.client.CustomObjectsApi", return_value=mock_custom_api),
            patch("k8si.operator.snapshot.time.sleep"),
        ):
            snap_mod._wait_snapshot_ready_sync("test-snap", "default")

        assert mock_custom_api.get_namespaced_custom_object.call_count == 4
