"""Tests for spec.notifyOnSuccess webhook in k8si/operator/main.py."""

from unittest.mock import AsyncMock, MagicMock, patch

from tests.helpers import SPEC, run_coro


def test_notify_called_on_backup_success():
    """_update_parent_backup calls _notify_webhook on success when notifyOnSuccess is set."""
    from k8si.operator.main import _update_parent_backup

    custom = MagicMock()
    spec = {**SPEC, "notifyOnSuccess": "http://hooks.example.com/ok"}
    backup_obj = {"status": {}}
    run_result = {"lastBackupTime": "2026-06-12T02:00:00+00:00", "message": ""}

    async def _run():
        with (
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main._notify_webhook", new_callable=AsyncMock) as mock_notify,
        ):
            await _update_parent_backup(
                custom, "test", "default", "test-run", "success", run_result, backup_obj, spec, 30
            )
            mock_notify.assert_called_once()
            call_args = mock_notify.call_args
            assert call_args.args[0] == "http://hooks.example.com/ok"
            payload = call_args.args[1]
            assert payload["name"] == "test"
            assert payload["namespace"] == "default"
            assert payload["result"] == "success"
            assert "duration" in payload

    run_coro(_run())


def test_notify_not_called_on_failure_when_only_success_configured():
    """_update_parent_backup does NOT notify for failure when only notifyOnSuccess is set."""
    from k8si.operator.main import _update_parent_backup

    custom = MagicMock()
    spec = {**SPEC, "notifyOnSuccess": "http://hooks.example.com/ok"}
    backup_obj = {"status": {}}

    async def _run():
        with (
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main._notify_webhook", new_callable=AsyncMock) as mock_notify,
        ):
            await _update_parent_backup(
                custom,
                "test",
                "default",
                "test-run",
                "failed",
                {},
                backup_obj,
                spec,
                30,
                error="disk full",
            )
            mock_notify.assert_not_called()

    run_coro(_run())


def test_webhook_payload_includes_duration():
    """Webhook payload includes duration on success."""
    from k8si.operator.main import _update_parent_backup

    custom = MagicMock()
    spec = {**SPEC, "notifyOnSuccess": "http://hooks.example.com/ok"}
    backup_obj = {"status": {}}
    run_result = {"lastBackupTime": "2026-06-12T02:00:00+00:00", "message": ""}

    async def _run():
        with (
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main._notify_webhook", new_callable=AsyncMock) as mock_notify,
        ):
            await _update_parent_backup(
                custom, "test", "default", "test-run", "success", run_result, backup_obj, spec, 42
            )
            payload = mock_notify.call_args.args[1]
            assert isinstance(payload["duration"], int)
            assert payload["duration"] == 42

    run_coro(_run())
