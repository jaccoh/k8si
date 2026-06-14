"""Tests for lastBackupDuration tracking — via _update_parent_backup in main.py."""

from unittest.mock import AsyncMock, MagicMock, patch

from tests.helpers import SPEC, run_coro


def _run_update_and_get_duration(result: str, duration: int, error: str = "") -> int | None:
    """Run _update_parent_backup and return the lastBackupDuration from the PATCH body."""
    from k8si.operator.main import _update_parent_backup

    custom = MagicMock()
    backup_obj = {"status": {}}
    run_result = {"lastBackupTime": "2026-06-12T02:00:00+00:00", "message": ""}

    async def _run():
        with (
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main._notify_webhook", new_callable=AsyncMock),
        ):
            await _update_parent_backup(
                custom,
                "test",
                "default",
                "test-run",
                result,
                run_result,
                backup_obj,
                SPEC,
                duration,
                error=error,
            )

    run_coro(_run())
    call = custom.patch_namespaced_custom_object_status.call_args
    body = call.args[5] if len(call.args) > 5 else call.kwargs.get("body", {})
    return body["status"].get("lastBackupDuration")


def test_duration_recorded_on_success():
    """_update_parent_backup records lastBackupDuration (int seconds) on success."""
    duration = _run_update_and_get_duration("success", 42)
    assert duration is not None, "lastBackupDuration must be set on success"
    assert isinstance(duration, int), "lastBackupDuration must be an integer"
    assert duration == 42


def test_duration_recorded_on_failure():
    """_update_parent_backup records lastBackupDuration even when the run failed."""
    duration = _run_update_and_get_duration("failed", 7, error="disk full")
    assert duration is not None, "lastBackupDuration must be set even on failure"
    assert isinstance(duration, int)
    assert duration == 7
