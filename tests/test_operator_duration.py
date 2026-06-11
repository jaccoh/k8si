"""Tests for lastBackupDuration tracking in backup_timer."""

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


def test_duration_recorded_on_success():
    """backup_timer records lastBackupDuration (int seconds) on success."""
    from k8si.operator import main

    patch_obj = _Patch()
    logger = logging.getLogger("test")

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=True),
        ):
            mock_run.return_value = {
                "lastBackupResult": "success",
                "lastBackupTime": "2026-06-12T02:00:00+00:00",
                "message": "",
            }
            await main.backup_timer(
                body=_BODY,
                spec=_SPEC,
                name="test",
                namespace="default",
                status={},
                patch=patch_obj,
                logger=logger,
            )

        duration = patch_obj.status.get("lastBackupDuration")
        assert duration is not None, "lastBackupDuration must be set on success"
        assert isinstance(duration, int), "lastBackupDuration must be an integer"
        assert duration >= 0

    _run(_run_timer())


def test_duration_recorded_on_failure():
    """backup_timer records lastBackupDuration even when backup fails."""
    from k8si.operator import main

    patch_obj = _Patch()
    logger = logging.getLogger("test")

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=True),
            patch("k8si.operator.main._notify_webhook", new_callable=AsyncMock),
        ):
            mock_run.side_effect = RuntimeError("disk full")
            await main.backup_timer(
                body=_BODY,
                spec={**_SPEC, "notifyOnFailure": "http://hook/"},
                name="test",
                namespace="default",
                status={},
                patch=patch_obj,
                logger=logger,
            )

        duration = patch_obj.status.get("lastBackupDuration")
        assert duration is not None, "lastBackupDuration must be set even on failure"
        assert isinstance(duration, int)
        assert duration >= 0

    _run(_run_timer())
