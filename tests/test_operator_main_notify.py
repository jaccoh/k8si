"""Tests for spec.notifyOnFailure webhook notifications in k8si/operator/main.py."""

from unittest.mock import AsyncMock, MagicMock, patch

from tests.helpers import SPEC, run_coro

# ── unit tests for _notify_webhook ────────────────────────────────────────────


def test_notify_webhook_posts_to_url():
    from k8si.operator.main import _notify_webhook

    async def _run_notify():
        with patch("k8si.operator.main.httpx.post") as mock_post:
            await _notify_webhook(
                "http://hooks.example.com/alert",
                {"name": "test", "result": "failed"},
            )
            mock_post.assert_called_once_with(
                "http://hooks.example.com/alert",
                json={"name": "test", "result": "failed"},
                timeout=10.0,
            )

    run_coro(_run_notify())


def test_notify_webhook_swallows_http_errors():
    from k8si.operator.main import _notify_webhook

    async def _run_notify():
        with patch("k8si.operator.main.httpx.post", side_effect=Exception("connection refused")):
            # Must not raise — webhook failures are best-effort
            await _notify_webhook("http://bad-host/", {"name": "test"})

    run_coro(_run_notify())


# ── integration: _update_parent_backup notifies on failure when notifyOnFailure set ──


def test_notify_called_on_backup_failure():
    """_update_parent_backup calls _notify_webhook on failure when notifyOnFailure is set."""
    from k8si.operator.main import _update_parent_backup

    custom = MagicMock()
    spec = {**SPEC, "notifyOnFailure": "http://hooks.example.com/alert"}
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
            mock_notify.assert_called_once()
            call_args = mock_notify.call_args
            assert call_args.args[0] == "http://hooks.example.com/alert"
            payload = call_args.args[1]
            assert payload["name"] == "test"
            assert payload["namespace"] == "default"
            assert payload["result"] == "failed"
            assert "message" in payload

    run_coro(_run())


def test_notify_not_called_on_success_when_only_failure_configured():
    """_update_parent_backup does NOT notify for success when only notifyOnFailure is set."""
    from k8si.operator.main import _update_parent_backup

    custom = MagicMock()
    spec = {**SPEC, "notifyOnFailure": "http://hooks.example.com/alert"}
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
            mock_notify.assert_not_called()

    run_coro(_run())


def test_notify_not_called_when_not_configured():
    """_update_parent_backup does NOT call _notify_webhook when notifyOnFailure is absent."""
    from k8si.operator.main import _update_parent_backup

    custom = MagicMock()
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
                SPEC,
                30,
                error="disk full",
            )
            mock_notify.assert_not_called()

    run_coro(_run())
