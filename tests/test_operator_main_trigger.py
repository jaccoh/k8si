"""Tests for manual backup trigger via status.triggeredAt in k8si/operator/main.py."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

from tests.helpers import BODY, SPEC, FakePatch, run_coro

_TRIGGERED_AT = "2026-06-11T12:00:00+00:00"
_LAST_BACKUP = "2026-06-10T02:00:00+00:00"

_SUCCESS_RESULT = {
    "lastBackupResult": "success",
    "lastBackupTime": "2026-06-11T12:30:00+00:00",
    "message": "",
}


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
    """triggeredAt newer than lastBackupTime creates a K8siBackupRun even if _is_due is False."""
    from k8si.operator import main

    patch_obj = FakePatch()
    logger = logging.getLogger("test")

    async def _run_timer():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=False),
        ):
            mock_k8s = MagicMock()
            mock_k8s_cls.return_value = mock_k8s
            await main.backup_timer(
                body=BODY,
                spec=SPEC,
                name="test",
                namespace="default",
                status={"triggeredAt": _TRIGGERED_AT, "lastBackupTime": _LAST_BACKUP},
                patch=patch_obj,
                logger=logger,
            )
            mock_k8s.create_namespaced_custom_object.assert_called_once()

    run_coro(_run_timer())


def test_triggered_bypasses_window():
    """triggeredAt creates a K8siBackupRun even when outside the configured backupWindow."""
    from k8si.operator import main

    patch_obj = FakePatch()
    logger = logging.getLogger("test")
    spec = {**SPEC, "backupWindow": {"start": "02:00", "end": "04:00"}}

    async def _run_timer():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=False),
            patch("k8si.operator.main._is_in_window", return_value=False),
        ):
            mock_k8s = MagicMock()
            mock_k8s_cls.return_value = mock_k8s
            await main.backup_timer(
                body=BODY,
                spec=spec,
                name="test",
                namespace="default",
                status={"triggeredAt": _TRIGGERED_AT, "lastBackupTime": _LAST_BACKUP},
                patch=patch_obj,
                logger=logger,
            )
            mock_k8s.create_namespaced_custom_object.assert_called_once()

    run_coro(_run_timer())


def test_paused_blocks_trigger():
    """spec.paused=True prevents backup even when triggeredAt is set."""
    from k8si.operator import main

    patch_obj = FakePatch()
    logger = logging.getLogger("test")
    spec = {**SPEC, "paused": True}

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
        ):
            await main.backup_timer(
                body=BODY,
                spec=spec,
                name="test",
                namespace="default",
                status={"triggeredAt": _TRIGGERED_AT, "lastBackupTime": _LAST_BACKUP},
                patch=patch_obj,
                logger=logger,
            )
            mock_run.assert_not_called()

    run_coro(_run_timer())


def test_trigger_cleared_on_run_create():
    """triggeredAt is cleared from patch.status as soon as the run is queued."""
    from k8si.operator import main

    patch_obj = FakePatch()
    logger = logging.getLogger("test")

    async def _run_timer():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=False),
        ):
            mock_k8s_cls.return_value = MagicMock()
            await main.backup_timer(
                body=BODY,
                spec=SPEC,
                name="test",
                namespace="default",
                status={"triggeredAt": _TRIGGERED_AT, "lastBackupTime": _LAST_BACKUP},
                patch=patch_obj,
                logger=logger,
            )

    run_coro(_run_timer())

    assert patch_obj.status.get("triggeredAt") is None


def test_trigger_cleared_even_when_create_fails():
    """triggeredAt is cleared before the run create attempt, so it stays cleared on failure."""
    from k8si.operator import main

    patch_obj = FakePatch()
    logger = logging.getLogger("test")

    async def _run_timer():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main._is_due", return_value=False),
        ):
            mock_k8s = MagicMock()
            mock_k8s.create_namespaced_custom_object.side_effect = RuntimeError("API down")
            mock_k8s_cls.return_value = mock_k8s
            await main.backup_timer(
                body=BODY,
                spec=SPEC,
                name="test",
                namespace="default",
                status={"triggeredAt": _TRIGGERED_AT, "lastBackupTime": _LAST_BACKUP},
                patch=patch_obj,
                logger=logger,
            )

    run_coro(_run_timer())

    assert patch_obj.status.get("triggeredAt") is None
