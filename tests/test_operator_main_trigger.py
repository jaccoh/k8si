"""Tests for manual backup trigger via status.triggeredAt in k8si/operator/main.py."""

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

_SUCCESS_RESULT = {
    "lastBackupResult": "success",
    "lastBackupTime": "2026-06-11T12:30:00+00:00",
    "message": "",
}

# triggeredAt is newer than _LAST_BACKUP so it counts as a pending trigger
_TRIGGERED_AT = "2026-06-11T12:00:00+00:00"
_LAST_BACKUP = "2026-06-10T02:00:00+00:00"


# ── unit tests for _is_manual_trigger ─────────────────────────────────────────


def test_no_triggered_at_is_false():
    from k8si.operator.main import _is_manual_trigger

    assert _is_manual_trigger(None, None) is False
    assert _is_manual_trigger("", None) is False


def test_triggered_at_with_no_last_backup_is_true():
    from k8si.operator.main import _is_manual_trigger

    assert _is_manual_trigger(_TRIGGERED_AT, None) is True


def test_triggered_at_newer_than_last_backup_is_true():
    from k8si.operator.main import _is_manual_trigger

    assert _is_manual_trigger(_TRIGGERED_AT, _LAST_BACKUP) is True


def test_triggered_at_older_than_last_backup_is_false():
    """triggeredAt older than lastBackupTime means the trigger was already consumed."""
    from k8si.operator.main import _is_manual_trigger

    assert _is_manual_trigger(_LAST_BACKUP, _TRIGGERED_AT) is False


def test_invalid_triggered_at_is_false():
    from k8si.operator.main import _is_manual_trigger

    assert _is_manual_trigger("notadate", None) is False


# ── integration tests: backup_timer trigger behaviour ────────────────────────


def test_triggered_bypasses_schedule():
    """triggeredAt newer than lastBackupTime causes backup to run even if _is_due is False."""
    from k8si.operator import main

    patch_obj = _Patch()
    logger = logging.getLogger("test")

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=False),
        ):
            mock_run.return_value = _SUCCESS_RESULT
            await main.backup_timer(
                body=_BODY,
                spec=_SPEC,
                name="test",
                namespace="default",
                status={"triggeredAt": _TRIGGERED_AT, "lastBackupTime": _LAST_BACKUP},
                patch=patch_obj,
                logger=logger,
            )
            mock_run.assert_called_once()

    _run(_run_timer())


def test_triggered_bypasses_window():
    """triggeredAt causes backup to run even when outside the configured backupWindow."""
    from k8si.operator import main

    patch_obj = _Patch()
    logger = logging.getLogger("test")
    spec = {**_SPEC, "backupWindow": {"start": "02:00", "end": "04:00"}}

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=False),
            patch("k8si.operator.main._is_in_window", return_value=False),
        ):
            mock_run.return_value = _SUCCESS_RESULT
            await main.backup_timer(
                body=_BODY,
                spec=spec,
                name="test",
                namespace="default",
                status={"triggeredAt": _TRIGGERED_AT, "lastBackupTime": _LAST_BACKUP},
                patch=patch_obj,
                logger=logger,
            )
            mock_run.assert_called_once()

    _run(_run_timer())


def test_paused_blocks_trigger():
    """spec.paused=True prevents backup even when triggeredAt is set."""
    from k8si.operator import main

    patch_obj = _Patch()
    logger = logging.getLogger("test")
    spec = {**_SPEC, "paused": True}

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
        ):
            await main.backup_timer(
                body=_BODY,
                spec=spec,
                name="test",
                namespace="default",
                status={"triggeredAt": _TRIGGERED_AT, "lastBackupTime": _LAST_BACKUP},
                patch=patch_obj,
                logger=logger,
            )
            mock_run.assert_not_called()

    _run(_run_timer())


def test_trigger_cleared_on_success():
    """After a triggered backup succeeds, patch.status['triggeredAt'] is None."""
    from k8si.operator import main

    patch_obj = _Patch()
    logger = logging.getLogger("test")

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=False),
        ):
            mock_run.return_value = _SUCCESS_RESULT
            await main.backup_timer(
                body=_BODY,
                spec=_SPEC,
                name="test",
                namespace="default",
                status={"triggeredAt": _TRIGGERED_AT, "lastBackupTime": _LAST_BACKUP},
                patch=patch_obj,
                logger=logger,
            )

    _run(_run_timer())

    assert patch_obj.status.get("triggeredAt") is None


def test_trigger_cleared_on_failure():
    """After a triggered backup fails, patch.status['triggeredAt'] is still None."""
    from k8si.operator import main

    patch_obj = _Patch()
    logger = logging.getLogger("test")

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=False),
        ):
            mock_run.side_effect = RuntimeError("disk full")
            await main.backup_timer(
                body=_BODY,
                spec=_SPEC,
                name="test",
                namespace="default",
                status={"triggeredAt": _TRIGGERED_AT, "lastBackupTime": _LAST_BACKUP},
                patch=patch_obj,
                logger=logger,
            )

    _run(_run_timer())

    assert patch_obj.status.get("triggeredAt") is None
