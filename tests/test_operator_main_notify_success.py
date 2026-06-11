"""Tests for spec.notifyOnSuccess webhook in k8si/operator/main.py."""

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


def test_notify_called_on_backup_success():
    """backup_timer calls _notify_webhook when spec.notifyOnSuccess is set and backup succeeds."""
    from k8si.operator import main

    patch_obj = _Patch()
    logger = logging.getLogger("test")
    spec = {**_SPEC, "notifyOnSuccess": "http://hooks.example.com/ok"}

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
            assert call_args.args[0] == "http://hooks.example.com/ok"
            payload = call_args.args[1]
            assert payload["name"] == "test"
            assert payload["namespace"] == "default"
            assert payload["result"] == "success"
            assert "duration" in payload

    _run(_run_timer())


def test_notify_not_called_on_failure_when_only_success_configured():
    """backup_timer does NOT call _notify_webhook for failure when only notifyOnSuccess is set."""
    from k8si.operator import main

    patch_obj = _Patch()
    logger = logging.getLogger("test")
    spec = {**_SPEC, "notifyOnSuccess": "http://hooks.example.com/ok"}

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


def test_webhook_payload_includes_duration():
    """Webhook payload includes lastBackupDuration on success."""
    from k8si.operator import main

    patch_obj = _Patch()
    logger = logging.getLogger("test")
    spec = {
        **_SPEC,
        "notifyOnSuccess": "http://hooks.example.com/ok",
        "notifyOnFailure": "http://hooks.example.com/err",
    }

    async def _run_success():
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
                body=_BODY,
                spec=spec,
                name="test",
                namespace="default",
                status={},
                patch=patch_obj,
                logger=logger,
            )
            payload = mock_notify.call_args.args[1]
            assert isinstance(payload["duration"], int)
            assert payload["duration"] >= 0

    _run(_run_success())
