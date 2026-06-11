"""Tests for spec.notifyOnFailure webhook notifications in k8si/operator/main.py."""

import asyncio
import logging
from unittest.mock import AsyncMock, patch


def _run(coro):
    return asyncio.run(coro)


class _StatusDict(dict):
    def update(self, other=None, **kwargs):  # type: ignore[override]
        if other:
            super().update(other)
        super().update(kwargs)


class _Patch:
    def __init__(self):
        self.status = _StatusDict()


_SPEC = {"schedule": "0 2 * * *", "pvc": "test-pvc", "resticSecret": "test-secret"}
_BODY = {"metadata": {"name": "test", "namespace": "default"}}


# ── unit tests for _notify_failure ────────────────────────────────────────────


def test_notify_failure_posts_to_webhook():
    from k8si.operator.main import _notify_failure

    async def _run_notify():
        with patch("httpx.post") as mock_post:
            await _notify_failure(
                "http://hooks.example.com/alert",
                {"name": "test", "result": "failed"},
            )
            mock_post.assert_called_once_with(
                "http://hooks.example.com/alert",
                json={"name": "test", "result": "failed"},
                timeout=10.0,
            )

    _run(_run_notify())


def test_notify_failure_swallows_http_errors():
    from k8si.operator.main import _notify_failure

    async def _run_notify():
        with patch("httpx.post", side_effect=Exception("connection refused")):
            # Must not raise — webhook failures are best-effort
            await _notify_failure("http://bad-host/", {"name": "test"})

    _run(_run_notify())


# ── integration: backup_timer notifies on failure when notifyOnFailure set ────


def test_notify_called_on_backup_failure():
    """backup_timer calls _notify_failure when spec.notifyOnFailure is set and backup fails."""
    from k8si.operator import main

    patch_obj = _Patch()
    logger = logging.getLogger("test")
    spec = {**_SPEC, "notifyOnFailure": "http://hooks.example.com/alert"}

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=True),
            patch("k8si.operator.main._notify_failure", new_callable=AsyncMock) as mock_notify,
        ):
            mock_run.side_effect = RuntimeError("disk full")
            await main.backup_timer(
                body=_BODY,
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

    _run(_run_timer())


def test_notify_not_called_on_success():
    """backup_timer does NOT call _notify_failure when backup succeeds."""
    from k8si.operator import main

    patch_obj = _Patch()
    logger = logging.getLogger("test")
    spec = {**_SPEC, "notifyOnFailure": "http://hooks.example.com/alert"}

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=True),
            patch("k8si.operator.main._notify_failure", new_callable=AsyncMock) as mock_notify,
        ):
            mock_run.return_value = {
                "lastBackupResult": "success",
                "lastBackupTime": "2026-06-12T02:00:00+00:00",
                "message": "",
            }
            await main.backup_timer(
                body=_BODY,
                spec=spec,
                name="test",
                namespace="default",
                status={},
                patch=patch_obj,
                logger=logger,
            )
            mock_notify.assert_not_called()

    _run(_run_timer())


def test_notify_not_called_when_not_configured():
    """backup_timer does NOT call _notify_failure when notifyOnFailure is absent."""
    from k8si.operator import main

    patch_obj = _Patch()
    logger = logging.getLogger("test")

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=True),
            patch("k8si.operator.main._notify_failure", new_callable=AsyncMock) as mock_notify,
        ):
            mock_run.side_effect = RuntimeError("disk full")
            await main.backup_timer(
                body=_BODY,
                spec=_SPEC,  # no notifyOnFailure
                name="test",
                namespace="default",
                status={},
                patch=patch_obj,
                logger=logger,
            )
            mock_notify.assert_not_called()

    _run(_run_timer())
