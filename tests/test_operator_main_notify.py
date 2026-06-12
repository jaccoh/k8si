"""Tests for spec.notifyOnFailure webhook notifications in k8si/operator/main.py."""

import logging
from unittest.mock import AsyncMock, patch

from tests.helpers import BODY, SPEC, FakePatch, run_coro

# ── unit tests for _notify_webhook ────────────────────────────────────────────


def test_notify_webhook_posts_to_url():
    from k8si.operator.main import _notify_webhook

    async def _run_notify():
        with patch("httpx.post") as mock_post:
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
        with patch("httpx.post", side_effect=Exception("connection refused")):
            # Must not raise — webhook failures are best-effort
            await _notify_webhook("http://bad-host/", {"name": "test"})

    run_coro(_run_notify())


# ── integration: backup_timer notifies on failure when notifyOnFailure set ────


def test_notify_called_on_backup_failure():
    """backup_timer calls _notify_webhook when spec.notifyOnFailure is set and backup fails."""
    from k8si.operator import main

    patch_obj = FakePatch()
    logger = logging.getLogger("test")
    spec = {**SPEC, "notifyOnFailure": "http://hooks.example.com/alert"}

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=True),
            patch("k8si.operator.main._notify_webhook", new_callable=AsyncMock) as mock_notify,
        ):
            mock_run.side_effect = RuntimeError("disk full")
            await main.backup_timer(
                body=BODY,
                spec=spec,
                name="test",
                namespace="default",
                status={},
                patch=patch_obj,
                logger=logger,
            )
            mock_notify.assert_called_once()
            call_args = mock_notify.call_args
            assert call_args.args[0] == "http://hooks.example.com/alert"
            payload = call_args.args[1]
            assert payload["name"] == "test"
            assert payload["namespace"] == "default"
            assert payload["result"] == "failed"
            assert "message" in payload

    run_coro(_run_timer())


def test_notify_not_called_on_success_when_only_failure_configured():
    """backup_timer does NOT call _notify_webhook for success when only notifyOnFailure is set."""
    from k8si.operator import main

    patch_obj = FakePatch()
    logger = logging.getLogger("test")
    spec = {**SPEC, "notifyOnFailure": "http://hooks.example.com/alert"}

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=True),
            patch("k8si.operator.main._notify_webhook", new_callable=AsyncMock) as mock_notify,
        ):
            mock_run.return_value = {
                "lastBackupResult": "success",
                "lastBackupTime": "2026-06-12T02:00:00+00:00",
                "message": "",
            }
            await main.backup_timer(
                body=BODY,
                spec=spec,
                name="test",
                namespace="default",
                status={},
                patch=patch_obj,
                logger=logger,
            )
            mock_notify.assert_not_called()

    run_coro(_run_timer())


def test_notify_not_called_when_not_configured():
    """backup_timer does NOT call _notify_webhook when notifyOnFailure is absent."""
    from k8si.operator import main

    patch_obj = FakePatch()
    logger = logging.getLogger("test")

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=True),
            patch("k8si.operator.main._notify_webhook", new_callable=AsyncMock) as mock_notify,
        ):
            mock_run.side_effect = RuntimeError("disk full")
            await main.backup_timer(
                body=BODY,
                spec=SPEC,  # no notifyOnFailure
                name="test",
                namespace="default",
                status={},
                patch=patch_obj,
                logger=logger,
            )
            mock_notify.assert_not_called()

    run_coro(_run_timer())
