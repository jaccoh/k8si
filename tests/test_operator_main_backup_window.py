"""Tests for spec.backupWindow (time-of-day restriction) in k8si/operator/main.py."""

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from tests.helpers import BODY, SPEC, FakePatch, run_coro

_SUCCESS_RESULT = {
    "lastBackupResult": "success",
    "lastBackupTime": "2026-06-11T02:00:00+00:00",
    "message": "",
}


# ── unit tests for _is_in_window ──────────────────────────────────────────────


def test_no_window_always_allowed():
    from k8si.operator.main import _is_in_window

    assert _is_in_window({}) is True


def test_inside_window():
    from k8si.operator.main import _is_in_window

    now = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)
    assert _is_in_window({"start": "02:00", "end": "06:00"}, now) is True


def test_outside_window_before_start():
    from k8si.operator.main import _is_in_window

    now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    assert _is_in_window({"start": "02:00", "end": "06:00"}, now) is False


def test_outside_window_after_end():
    from k8si.operator.main import _is_in_window

    now = datetime(2026, 1, 1, 7, 0, tzinfo=UTC)
    assert _is_in_window({"start": "02:00", "end": "06:00"}, now) is False


def test_window_end_is_exclusive():
    from k8si.operator.main import _is_in_window

    now = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    assert _is_in_window({"start": "02:00", "end": "06:00"}, now) is False


def test_midnight_wrap_inside_before_midnight():
    from k8si.operator.main import _is_in_window

    now = datetime(2026, 1, 1, 23, 0, tzinfo=UTC)
    assert _is_in_window({"start": "22:00", "end": "04:00"}, now) is True


def test_midnight_wrap_inside_after_midnight():
    from k8si.operator.main import _is_in_window

    now = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    assert _is_in_window({"start": "22:00", "end": "04:00"}, now) is True


def test_midnight_wrap_outside():
    from k8si.operator.main import _is_in_window

    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert _is_in_window({"start": "22:00", "end": "04:00"}, now) is False


def test_invalid_window_permits_backup():
    from k8si.operator.main import _is_in_window

    now = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)
    assert _is_in_window({"start": "notavalid", "end": "06:00"}, now) is True


# ── integration tests: backup_timer skips outside window ─────────────────────


def test_outside_window_skips_backup():
    """backup_timer must not run a backup when outside the configured window."""
    from k8si.operator import main

    patch_obj = FakePatch()
    logger = logging.getLogger("test")
    spec = {**SPEC, "backupWindow": {"start": "02:00", "end": "04:00"}}

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main._is_due", return_value=True),
            patch("k8si.operator.main._is_in_window", return_value=False),
        ):
            await main.backup_timer(
                body=BODY,
                spec=spec,
                name="test",
                namespace="default",
                status={},
                patch=patch_obj,
                logger=logger,
            )
            mock_run.assert_not_called()

    run_coro(_run_timer())


def test_inside_window_runs_backup():
    """backup_timer must create a K8siBackupRun when inside the configured window."""
    from k8si.operator import main

    patch_obj = FakePatch()
    logger = logging.getLogger("test")
    spec = {**SPEC, "backupWindow": {"start": "02:00", "end": "04:00"}}

    async def _run_timer():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=True),
            patch("k8si.operator.main._is_in_window", return_value=True),
        ):
            mock_k8s = MagicMock()
            mock_k8s_cls.return_value = mock_k8s
            await main.backup_timer(
                body=BODY,
                spec=spec,
                name="test",
                namespace="default",
                status={},
                patch=patch_obj,
                logger=logger,
            )
            mock_k8s.create_namespaced_custom_object.assert_called_once()

    run_coro(_run_timer())
