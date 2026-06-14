"""Tests for recentBackups rolling history — now via _update_parent_backup in main.py."""

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from tests.helpers import BODY, SPEC, FakePatch, run_coro

_LAST_BACKUP_TIME = "2026-06-09T02:00:00+00:00"


def _run_update(
    result: str,
    existing_recent: list,
    error: str = "",
    run_result: dict | None = None,
) -> list:
    """Run _update_parent_backup and return the recentBackups list from the PATCH call."""
    from k8si.operator.main import _update_parent_backup

    custom = MagicMock()
    backup_obj = {"status": {"recentBackups": existing_recent}}
    run_result = run_result or {"lastBackupTime": _LAST_BACKUP_TIME, "message": ""}

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
                30,
                error=error,
            )

    run_coro(_run())
    call = custom.patch_namespaced_custom_object_status.call_args
    body = call.args[5] if len(call.args) > 5 else call.kwargs.get("body", {})
    return body["status"]["recentBackups"]


# ---------------------------------------------------------------------------
# Test 1 — success prepends a "success" entry to recentBackups
# ---------------------------------------------------------------------------


def test_success_prepends_to_recent_backups():
    existing_entry = {"time": "2026-06-08T02:00:00+00:00", "result": "success"}

    recent = _run_update("success", [existing_entry])

    assert len(recent) == 2, f"Expected 2 entries, got {len(recent)}"
    assert recent[0]["result"] == "success"
    assert recent[0]["time"] == _LAST_BACKUP_TIME
    assert recent[1] == existing_entry


# ---------------------------------------------------------------------------
# Test 2 — failure prepends a "failed" entry to recentBackups
# ---------------------------------------------------------------------------


def test_failure_prepends_to_recent_backups():
    existing_entry = {"time": "2026-06-08T02:00:00+00:00", "result": "success"}

    recent = _run_update("failed", [existing_entry], error="disk full")

    assert len(recent) == 2, f"Expected 2 entries, got {len(recent)}"
    assert recent[0]["result"] == "failed"
    assert "time" in recent[0]
    assert recent[1] == existing_entry


# ---------------------------------------------------------------------------
# Test 3 — recentBackups is trimmed to 30 entries
# ---------------------------------------------------------------------------


def test_recent_backups_trimmed_to_30():
    existing_entries = [
        {"time": f"2026-06-0{(i % 9) + 1}T0{i % 10}:00:00+00:00", "result": "success"}
        for i in range(30)
    ]

    recent = _run_update("success", list(existing_entries))

    assert len(recent) == 30, f"Expected exactly 30 entries, got {len(recent)}"
    assert recent[0]["time"] == _LAST_BACKUP_TIME
    assert recent[0]["result"] == "success"
    assert recent[-1] != existing_entries[-1]


# ---------------------------------------------------------------------------
# Test 4 — maxRetriesPerDay skips backup when daily failure limit is reached
# ---------------------------------------------------------------------------


def _today_iso() -> str:
    return datetime.now(tz=UTC).date().isoformat()


def test_max_retries_per_day_skips_when_limit_reached():
    from k8si.operator import main

    patch_obj = FakePatch()
    logger = logging.getLogger("test")

    today = _today_iso()
    failed_entries = [
        {"time": f"{today}T01:00:00+00:00", "result": "failed"},
        {"time": f"{today}T02:00:00+00:00", "result": "failed"},
        {"time": f"{today}T03:00:00+00:00", "result": "failed"},
    ]
    spec_with_limit = {**SPEC, "maxRetriesPerDay": 3}

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main._is_due", return_value=True),
        ):
            await main.backup_timer(
                body=BODY,
                spec=spec_with_limit,
                name="test",
                namespace="default",
                status={"recentBackups": failed_entries},
                patch=patch_obj,
                logger=logger,
            )
            mock_run.assert_not_called()

    run_coro(_run_timer())


def test_max_retries_per_day_runs_when_under_limit():
    from k8si.operator import main

    logger = logging.getLogger("test")

    today = _today_iso()
    failed_entries = [
        {"time": f"{today}T01:00:00+00:00", "result": "failed"},
        {"time": f"{today}T02:00:00+00:00", "result": "failed"},
    ]
    spec_with_limit = {**SPEC, "maxRetriesPerDay": 3}

    async def _run_timer():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=True),
        ):
            mock_k8s = MagicMock()
            mock_k8s_cls.return_value = mock_k8s
            await main.backup_timer(
                body=BODY,
                spec=spec_with_limit,
                name="test",
                namespace="default",
                status={"recentBackups": failed_entries},
                patch=FakePatch(),
                logger=logger,
            )
            mock_k8s.create_namespaced_custom_object.assert_called_once()

    run_coro(_run_timer())


def test_max_retries_per_day_default_is_three():
    """Without maxRetriesPerDay in spec, default is 3."""
    from k8si.operator import main

    patch_obj = FakePatch()
    logger = logging.getLogger("test")

    today = _today_iso()
    failed_entries = [
        {"time": f"{today}T01:00:00+00:00", "result": "failed"},
        {"time": f"{today}T02:00:00+00:00", "result": "failed"},
        {"time": f"{today}T03:00:00+00:00", "result": "failed"},
    ]

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main._is_due", return_value=True),
        ):
            await main.backup_timer(
                body=BODY,
                spec=SPEC,  # no maxRetriesPerDay — uses default 3
                name="test",
                namespace="default",
                status={"recentBackups": failed_entries},
                patch=patch_obj,
                logger=logger,
            )
            mock_run.assert_not_called()

    run_coro(_run_timer())


# ---------------------------------------------------------------------------
# Test 8 — spec.paused skips backup entirely
# ---------------------------------------------------------------------------


def test_paused_skips_backup():
    """When spec.paused is True, backup_timer returns without running a backup."""
    from k8si.operator import main

    patch_obj = FakePatch()
    logger = logging.getLogger("test")
    spec_paused = {**SPEC, "paused": True}

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main._is_due", return_value=True),
        ):
            await main.backup_timer(
                body=BODY,
                spec=spec_paused,
                name="test",
                namespace="default",
                status={},
                patch=patch_obj,
                logger=logger,
            )
            mock_run.assert_not_called()

    run_coro(_run_timer())


def test_backup_timer_skips_when_active_run_exists_in_k8s():
    """backup_timer skips creating a run if _has_active_run_sync returns True.

    This guards against duplicate runs after an operator restart when the in-memory
    _running set is empty but K8siBackupRun resources are already Pending/Running.
    """
    from k8si.operator import main

    logger = logging.getLogger("test")

    async def _run_timer():
        with (
            patch("k8si.operator.main._has_active_run_sync", return_value=True),
            patch("k8si.operator.main._is_due", return_value=True),
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
        ):
            mock_k8s = MagicMock()
            mock_k8s_cls.return_value = mock_k8s
            await main.backup_timer(
                body=BODY,
                spec=SPEC,
                name="test",
                namespace="default",
                status={},
                patch=FakePatch(),
                logger=logger,
            )
            mock_k8s.create_namespaced_custom_object.assert_not_called()

    run_coro(_run_timer())
