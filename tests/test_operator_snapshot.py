"""Tests for k8si/operator/snapshot.py."""

import asyncio
from unittest.mock import MagicMock, patch

import kubernetes.client.exceptions
import pytest

import k8si.operator.snapshot as snap_mod

_CUSTOM_API = "k8si.operator.snapshot.kubernetes.client.CustomObjectsApi"


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

        with patch(_CUSTOM_API, return_value=mock_api):
            snap_mod._wait_no_snapshot_in_progress_sync("my-pvc", "default")

        assert mock_api.list_namespaced_custom_object.call_count == 1

    def test_proceeds_immediately_when_only_ready_snapshots_for_pvc(self):
        mock_api = MagicMock()
        mock_api.list_namespaced_custom_object.return_value = {
            "items": [_make_snapshot_item("my-pvc", ready=True)]
        }

        with patch(_CUSTOM_API, return_value=mock_api):
            snap_mod._wait_no_snapshot_in_progress_sync("my-pvc", "default")

        assert mock_api.list_namespaced_custom_object.call_count == 1

    def test_ignores_in_progress_snapshots_for_other_pvcs(self):
        mock_api = MagicMock()
        mock_api.list_namespaced_custom_object.return_value = {
            "items": [_make_snapshot_item("other-pvc", ready=False)]
        }

        with patch(_CUSTOM_API, return_value=mock_api):
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
            patch(_CUSTOM_API, return_value=mock_api),
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
            patch(_CUSTOM_API, return_value=mock_api),
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
            patch(_CUSTOM_API, return_value=mock_custom_api),
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
            patch(_CUSTOM_API, return_value=mock_custom_api),
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
            patch(_CUSTOM_API, return_value=mock_custom_api),
            patch("k8si.operator.snapshot.time.sleep"),
        ):
            snap_mod._wait_snapshot_ready_sync("test-snap", "default")

        assert mock_custom_api.get_namespaced_custom_object.call_count == 4


class TestWaitSnapshotReadySyncTimeout:
    def test_not_ready_then_ready_polls_twice(self):
        """Snapshot polled not-ready first, then ready — sleep(5) branch covered."""
        mock_api = MagicMock()
        mock_api.get_namespaced_custom_object.side_effect = [
            {"status": {"readyToUse": False}},
            {"status": {"readyToUse": True}},
        ]
        with (
            patch(_CUSTOM_API, return_value=mock_api),
            patch("k8si.operator.snapshot.time.sleep"),
        ):
            snap_mod._wait_snapshot_ready_sync("test-snap", "default")
        assert mock_api.get_namespaced_custom_object.call_count == 2

    def test_deadline_exceeded_raises_timeout(self):
        """Snapshot never becomes ready — TimeoutError raised when deadline passes."""
        mock_api = MagicMock()
        mock_api.get_namespaced_custom_object.return_value = {"status": {"readyToUse": False}}
        with (
            patch(_CUSTOM_API, return_value=mock_api),
            patch("k8si.operator.snapshot.time.sleep"),
            patch("k8si.operator.snapshot.time.monotonic", side_effect=[0.0, 0.0, 9999.0]),
        ):
            with pytest.raises(TimeoutError, match="not ready after"):
                snap_mod._wait_snapshot_ready_sync("test-snap", "default")


class TestGetPvcInfoSync:
    def test_returns_pvc_fields(self):
        pvc = MagicMock()
        pvc.spec.access_modes = ["ReadWriteOnce"]
        pvc.spec.resources.requests = {"storage": "10Gi"}
        pvc.spec.storage_class_name = "standard"
        mock_v1 = MagicMock()
        mock_v1.read_namespaced_persistent_volume_claim.return_value = pvc
        mock_custom = MagicMock()
        mock_custom.get_namespaced_custom_object.side_effect = Exception("not found")
        with (
            patch("k8si.operator.snapshot.kubernetes.client.CoreV1Api", return_value=mock_v1),
            patch(_CUSTOM_API, return_value=mock_custom),
        ):
            access_mode, storage, sc = snap_mod._get_pvc_info_sync("my-pvc", "my-snap", "default")
        assert access_mode == "ReadWriteOnce"
        assert storage == "10Gi"
        assert sc == "standard"

    def test_defaults_access_mode_when_missing(self):
        pvc = MagicMock()
        pvc.spec.access_modes = []
        pvc.spec.resources.requests = {"storage": "5Gi"}
        pvc.spec.storage_class_name = None
        mock_v1 = MagicMock()
        mock_v1.read_namespaced_persistent_volume_claim.return_value = pvc
        mock_custom = MagicMock()
        mock_custom.get_namespaced_custom_object.side_effect = Exception("not found")
        with (
            patch("k8si.operator.snapshot.kubernetes.client.CoreV1Api", return_value=mock_v1),
            patch(_CUSTOM_API, return_value=mock_custom),
        ):
            access_mode, _, sc = snap_mod._get_pvc_info_sync("my-pvc", "my-snap", "default")
        assert access_mode == "ReadWriteOnce"
        assert sc == ""

    def test_respects_snapshot_restore_size(self):
        pvc = MagicMock()
        pvc.spec.access_modes = ["ReadWriteOnce"]
        pvc.spec.resources.requests = {"storage": "10Gi"}
        pvc.spec.storage_class_name = "standard"
        mock_v1 = MagicMock()
        mock_v1.read_namespaced_persistent_volume_claim.return_value = pvc
        mock_custom = MagicMock()
        mock_custom.get_namespaced_custom_object.return_value = {
            "status": {"restoreSize": 314572800}
        }
        with (
            patch("k8si.operator.snapshot.kubernetes.client.CoreV1Api", return_value=mock_v1),
            patch(_CUSTOM_API, return_value=mock_custom),
        ):
            _, storage, _ = snap_mod._get_pvc_info_sync("my-pvc", "my-snap", "default")
        assert storage == "314572800"

    def test_respects_snapshot_restore_size_as_quantity_string(self):
        """LINSTOR CSI reports restoreSize as a k8s quantity string (e.g. '300Mi'),
        not a plain byte count -- the previous isdigit() gate silently dropped
        these and fell back to the source PVC's original request, causing the
        ephemeral restore PVC to be undersized and rejected by the provisioner.
        """
        pvc = MagicMock()
        pvc.spec.access_modes = ["ReadWriteOnce"]
        pvc.spec.resources.requests = {"storage": "128Mi"}
        pvc.spec.storage_class_name = "linstor-worker-local"
        mock_v1 = MagicMock()
        mock_v1.read_namespaced_persistent_volume_claim.return_value = pvc
        mock_custom = MagicMock()
        mock_custom.get_namespaced_custom_object.return_value = {"status": {"restoreSize": "300Mi"}}
        with (
            patch("k8si.operator.snapshot.kubernetes.client.CoreV1Api", return_value=mock_v1),
            patch(_CUSTOM_API, return_value=mock_custom),
        ):
            _, storage, _ = snap_mod._get_pvc_info_sync("my-pvc", "my-snap", "default")
        assert storage == "300Mi"


class TestCreateVolumeSnapshotSync:
    def test_calls_custom_api_with_body(self):
        mock_api = MagicMock()
        with patch(_CUSTOM_API, return_value=mock_api):
            snap_mod._create_volume_snapshot_sync("snap-1", "default", "my-pvc", "csi-snapclass")
        call_args = mock_api.create_namespaced_custom_object.call_args
        body = call_args[0][4]
        assert body["spec"]["volumeSnapshotClassName"] == "csi-snapclass"
        assert body["spec"]["source"]["persistentVolumeClaimName"] == "my-pvc"

    def test_omits_snapshot_class_when_none(self):
        mock_api = MagicMock()
        with patch(_CUSTOM_API, return_value=mock_api):
            snap_mod._create_volume_snapshot_sync("snap-1", "default", "my-pvc", None)
        call_args = mock_api.create_namespaced_custom_object.call_args
        body = call_args[0][4]
        assert "volumeSnapshotClassName" not in body["spec"]


class TestCreatePvcFromSnapshotSync:
    def test_calls_create_pvc_api(self):
        mock_v1 = MagicMock()
        with patch("k8si.operator.snapshot.kubernetes.client.CoreV1Api", return_value=mock_v1):
            snap_mod._create_pvc_from_snapshot_sync(
                "snap-pvc", "default", "snap-1", "ReadWriteOnce", "10Gi", "standard"
            )
        mock_v1.create_namespaced_persistent_volume_claim.assert_called_once()


class TestDeletePvcSync:
    def test_deletes_pvc_and_pv(self):
        pvc = MagicMock()
        pvc.spec.volume_name = "pv-abc"
        mock_v1 = MagicMock()
        mock_v1.read_namespaced_persistent_volume_claim.return_value = pvc
        with patch("k8si.operator.snapshot.kubernetes.client.CoreV1Api", return_value=mock_v1):
            snap_mod._delete_pvc_sync("my-pvc", "default")
        mock_v1.delete_namespaced_persistent_volume_claim.assert_called_once_with(
            "my-pvc", "default"
        )
        mock_v1.delete_persistent_volume.assert_called_once_with("pv-abc")

    def test_ignores_pv_delete_api_exception(self):
        """PV delete failure (e.g. already gone) is silently swallowed."""
        pvc = MagicMock()
        pvc.spec.volume_name = "pv-xyz"
        mock_v1 = MagicMock()
        mock_v1.read_namespaced_persistent_volume_claim.return_value = pvc
        exc = kubernetes.client.exceptions.ApiException(status=409)
        exc.status = 409
        mock_v1.delete_persistent_volume.side_effect = exc
        with patch("k8si.operator.snapshot.kubernetes.client.CoreV1Api", return_value=mock_v1):
            snap_mod._delete_pvc_sync("my-pvc", "default")  # must not raise

    def test_ignores_404_on_pvc_read(self):
        mock_v1 = MagicMock()
        exc = kubernetes.client.exceptions.ApiException(status=404)
        exc.status = 404
        mock_v1.read_namespaced_persistent_volume_claim.side_effect = exc
        with patch("k8si.operator.snapshot.kubernetes.client.CoreV1Api", return_value=mock_v1):
            snap_mod._delete_pvc_sync("my-pvc", "default")  # must not raise

    def test_reraises_non_404_on_pvc_read(self):
        mock_v1 = MagicMock()
        exc = kubernetes.client.exceptions.ApiException(status=500)
        exc.status = 500
        mock_v1.read_namespaced_persistent_volume_claim.side_effect = exc
        with patch("k8si.operator.snapshot.kubernetes.client.CoreV1Api", return_value=mock_v1):
            with pytest.raises(kubernetes.client.exceptions.ApiException):
                snap_mod._delete_pvc_sync("my-pvc", "default")


class TestDeleteVolumeSnapshotSync:
    def test_calls_delete_api(self):
        mock_api = MagicMock()
        with patch(_CUSTOM_API, return_value=mock_api):
            snap_mod._delete_volume_snapshot_sync("snap-1", "default")
        mock_api.delete_namespaced_custom_object.assert_called_once()

    def test_ignores_404(self):
        mock_api = MagicMock()
        exc = kubernetes.client.exceptions.ApiException(status=404)
        exc.status = 404
        mock_api.delete_namespaced_custom_object.side_effect = exc
        with patch(_CUSTOM_API, return_value=mock_api):
            snap_mod._delete_volume_snapshot_sync("snap-1", "default")  # must not raise

    def test_reraises_non_404(self):
        mock_api = MagicMock()
        exc = kubernetes.client.exceptions.ApiException(status=500)
        exc.status = 500
        mock_api.delete_namespaced_custom_object.side_effect = exc
        with patch(_CUSTOM_API, return_value=mock_api):
            with pytest.raises(kubernetes.client.exceptions.ApiException):
                snap_mod._delete_volume_snapshot_sync("snap-1", "default")


class TestAsyncOrchestrators:
    def test_create_snapshot_calls_all_steps(self):
        with (
            patch("k8si.operator.snapshot._wait_no_snapshot_in_progress_sync"),
            patch("k8si.operator.snapshot._create_volume_snapshot_sync"),
            patch("k8si.operator.snapshot._wait_snapshot_ready_sync"),
        ):
            asyncio.run(snap_mod.create_snapshot("snap-1", "default", "my-pvc", None))

    def test_create_pvc_from_snapshot_calls_api(self):
        with (
            patch(
                "k8si.operator.snapshot._get_pvc_info_sync",
                return_value=("ReadWriteOnce", "10Gi", "standard"),
            ),
            patch("k8si.operator.snapshot._create_pvc_from_snapshot_sync"),
        ):
            asyncio.run(
                snap_mod.create_pvc_from_snapshot("snap-pvc", "default", "snap-1", "my-pvc")
            )

    def test_delete_snapshot_and_pvc_deletes_both(self):
        with (
            patch("k8si.operator.snapshot._delete_pvc_sync") as mock_del_pvc,
            patch("k8si.operator.snapshot._delete_volume_snapshot_sync") as mock_del_snap,
        ):
            asyncio.run(snap_mod.delete_snapshot_and_pvc("default", "snap-1", "snap-pvc"))
        mock_del_pvc.assert_called_once_with("snap-pvc", "default")
        mock_del_snap.assert_called_once_with("snap-1", "default")

    def test_delete_snapshot_and_pvc_skips_pvc_when_none(self):
        with (
            patch("k8si.operator.snapshot._delete_pvc_sync") as mock_del_pvc,
            patch("k8si.operator.snapshot._delete_volume_snapshot_sync"),
        ):
            asyncio.run(snap_mod.delete_snapshot_and_pvc("default", "snap-1", None))
        mock_del_pvc.assert_not_called()
