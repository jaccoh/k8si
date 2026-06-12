"""Tests for recentBackups rolling history in k8si/operator/main.py backup_timer."""

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from tests.helpers import BODY, SPEC, FakePatch, run_coro

_SUCCESS_RESULT = {
    "lastBackupResult": "success",
    "lastBackupTime": "2026-06-09T02:00:00+00:00",
    "message": "",
}


# ---------------------------------------------------------------------------
# Test 1 — success prepends a "success" entry to recentBackups
# ---------------------------------------------------------------------------


def test_success_prepends_to_recent_backups():
    from k8si.operator import main

    patch_obj = FakePatch()
    logger = logging.getLogger("test")

    existing_entry = {"time": "2026-06-08T02:00:00+00:00", "result": "success"}

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=True),
        ):
            mock_run.return_value = _SUCCESS_RESULT
            await main.backup_timer(
                body=BODY,
                spec=SPEC,
                name="test",
                namespace="default",
                status={"recentBackups": [existing_entry]},
                patch=patch_obj,
                logger=logger,
            )

    run_coro(_run_timer())

    recent = patch_obj.status.get("recentBackups", [])
    assert len(recent) == 2, f"Expected 2 entries, got {len(recent)}"
    assert recent[0]["result"] == "success"
    assert recent[0]["time"] == _SUCCESS_RESULT["lastBackupTime"]
    assert recent[1] == existing_entry


# ---------------------------------------------------------------------------
# Test 2 — failure prepends a "failed" entry to recentBackups
# ---------------------------------------------------------------------------


def test_failure_prepends_to_recent_backups():
    from k8si.operator import main

    patch_obj = FakePatch()
    logger = logging.getLogger("test")

    existing_entry = {"time": "2026-06-08T02:00:00+00:00", "result": "success"}

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=True),
        ):
            mock_run.side_effect = RuntimeError("disk full")
            await main.backup_timer(
                body=BODY,
                spec=SPEC,
                name="test",
                namespace="default",
                status={"recentBackups": [existing_entry]},
                patch=patch_obj,
                logger=logger,
            )

    run_coro(_run_timer())

    recent = patch_obj.status.get("recentBackups", [])
    assert len(recent) == 2, f"Expected 2 entries, got {len(recent)}"
    assert recent[0]["result"] == "failed"
    assert "time" in recent[0]
    assert recent[1] == existing_entry


# ---------------------------------------------------------------------------
# Test 3 — recentBackups is trimmed to 30 entries
# ---------------------------------------------------------------------------


def test_recent_backups_trimmed_to_30():
    from k8si.operator import main

    patch_obj = FakePatch()
    logger = logging.getLogger("test")

    existing_entries = [
        {"time": f"2026-06-0{(i % 9) + 1}T0{i % 10}:00:00+00:00", "result": "success"}
        for i in range(30)
    ]

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=True),
        ):
            mock_run.return_value = _SUCCESS_RESULT
            await main.backup_timer(
                body=BODY,
                spec=SPEC,
                name="test",
                namespace="default",
                status={"recentBackups": list(existing_entries)},
                patch=patch_obj,
                logger=logger,
            )

    run_coro(_run_timer())

    recent = patch_obj.status.get("recentBackups", [])
    assert len(recent) == 30, f"Expected exactly 30 entries, got {len(recent)}"
    assert recent[0]["time"] == _SUCCESS_RESULT["lastBackupTime"]
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

    patch_obj = FakePatch()
    logger = logging.getLogger("test")

    today = _today_iso()
    failed_entries = [
        {"time": f"{today}T01:00:00+00:00", "result": "failed"},
        {"time": f"{today}T02:00:00+00:00", "result": "failed"},
    ]
    spec_with_limit = {**SPEC, "maxRetriesPerDay": 3}

    async def _run_timer():
        with (
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main.kopf.event"),
            patch("k8si.operator.main._is_due", return_value=True),
        ):
            mock_run.return_value = _SUCCESS_RESULT
            await main.backup_timer(
                body=BODY,
                spec=spec_with_limit,
                name="test",
                namespace="default",
                status={"recentBackups": failed_entries},
                patch=patch_obj,
                logger=logger,
            )
            mock_run.assert_called_once()

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
