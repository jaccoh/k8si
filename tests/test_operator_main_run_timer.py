"""Tests for the K8siBackupRun reconciliation timer in k8si/operator/main.py."""

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from tests.helpers import run_coro


def _body(phase: str, created_ago_min: int = 0, start_ago_min: int = 0) -> tuple[dict, dict]:
    created = (datetime.now(tz=UTC) - timedelta(minutes=created_ago_min)).isoformat()
    status: dict = {"phase": phase}
    if phase == "Running":
        status["startTime"] = (datetime.now(tz=UTC) - timedelta(minutes=start_ago_min)).isoformat()
    body = {"metadata": {"creationTimestamp": created}}
    return body, status


# ── terminal runs: no action ──────────────────────────────────────────────────


def test_run_timer_ignores_succeeded():
    from k8si.operator.main import run_reconcile_timer

    body, status = _body("Succeeded", created_ago_min=120)
    with patch("k8si.operator.main._patch_run_status") as mock_patch:
        run_coro(
            run_reconcile_timer(
                body=body, name="r", namespace="ns", status=status, logger=logging.getLogger()
            )
        )
    mock_patch.assert_not_called()


def test_run_timer_ignores_failed():
    from k8si.operator.main import run_reconcile_timer

    body, status = _body("Failed", created_ago_min=120)
    with patch("k8si.operator.main._patch_run_status") as mock_patch:
        run_coro(
            run_reconcile_timer(
                body=body, name="r", namespace="ns", status=status, logger=logging.getLogger()
            )
        )
    mock_patch.assert_not_called()


# ── Pending: age threshold ────────────────────────────────────────────────────


def test_run_timer_pending_young_no_action():
    from k8si.operator.main import run_reconcile_timer

    body, status = _body("Pending", created_ago_min=2)
    with patch("k8si.operator.main._patch_run_status") as mock_patch:
        run_coro(
            run_reconcile_timer(
                body=body, name="r", namespace="ns", status=status, logger=logging.getLogger()
            )
        )
    mock_patch.assert_not_called()


def test_run_timer_pending_old_marks_failed():
    from k8si.operator.main import run_reconcile_timer

    body, status = _body("Pending", created_ago_min=6)
    with patch(
        "k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock
    ) as mock_thread:
        run_coro(
            run_reconcile_timer(
                body=body, name="stuck-run", namespace="ns",
                status=status, logger=logging.getLogger()
            )
        )
    mock_thread.assert_awaited_once()
    patch_kwargs = mock_thread.call_args[0][3]
    assert patch_kwargs["phase"] == "Failed"
    assert "Pending" in patch_kwargs["message"]


# ── Running: age threshold ────────────────────────────────────────────────────


def test_run_timer_running_young_no_action():
    from k8si.operator.main import run_reconcile_timer

    body, status = _body("Running", start_ago_min=30)
    with patch("k8si.operator.main._patch_run_status") as mock_patch:
        run_coro(
            run_reconcile_timer(
                body=body, name="r", namespace="ns", status=status, logger=logging.getLogger()
            )
        )
    mock_patch.assert_not_called()


def test_run_timer_running_old_marks_failed():
    from k8si.operator.main import run_reconcile_timer

    body, status = _body("Running", start_ago_min=65)
    with patch(
        "k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock
    ) as mock_thread:
        run_coro(
            run_reconcile_timer(
                body=body, name="stuck-run", namespace="ns",
                status=status, logger=logging.getLogger()
            )
        )
    mock_thread.assert_awaited_once()
    patch_kwargs = mock_thread.call_args[0][3]
    assert patch_kwargs["phase"] == "Failed"
    assert "Running" in patch_kwargs["message"]


def test_run_timer_running_no_start_time_no_action():
    """Running run with no startTime in status: do nothing (can't determine age)."""
    from k8si.operator.main import run_reconcile_timer

    body = {"metadata": {"creationTimestamp": datetime.now(tz=UTC).isoformat()}}
    status = {"phase": "Running"}
    with patch("k8si.operator.main._patch_run_status") as mock_patch:
        run_coro(
            run_reconcile_timer(
                body=body, name="r", namespace="ns", status=status, logger=logging.getLogger()
            )
        )
    mock_patch.assert_not_called()


# ── completionTime and log entry ──────────────────────────────────────────────


def test_run_timer_sets_completion_time_when_pending_old():
    """Timer must include completionTime when marking a stuck Pending run Failed."""
    from k8si.operator.main import run_reconcile_timer

    body, status = _body("Pending", created_ago_min=6)
    with patch("k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
        run_coro(
            run_reconcile_timer(
                body=body, name="stuck", namespace="ns", status=status, logger=logging.getLogger()
            )
        )
    patch_kwargs = mock_thread.call_args[0][3]
    assert "completionTime" in patch_kwargs, "completionTime must be set on timer-killed runs"


def test_run_timer_sets_completion_time_when_running_old():
    """Timer must include completionTime when marking a stuck Running run Failed."""
    from k8si.operator.main import run_reconcile_timer

    body, status = _body("Running", start_ago_min=65)
    with patch("k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
        run_coro(
            run_reconcile_timer(
                body=body, name="stuck", namespace="ns", status=status, logger=logging.getLogger()
            )
        )
    patch_kwargs = mock_thread.call_args[0][3]
    assert "completionTime" in patch_kwargs, "completionTime must be set on timer-killed runs"


# ── parent backup update ──────────────────────────────────────────────────────


def _body_with_labels(
    phase: str, backup_name: str, created_ago_min: int = 0, start_ago_min: int = 0
) -> tuple[dict, dict]:
    created = (datetime.now(tz=UTC) - timedelta(minutes=created_ago_min)).isoformat()
    status: dict = {"phase": phase}
    if phase == "Running":
        status["startTime"] = (datetime.now(tz=UTC) - timedelta(minutes=start_ago_min)).isoformat()
    body = {
        "metadata": {
            "creationTimestamp": created,
            "labels": {"k8si.io/backup": backup_name},
        }
    }
    return body, status


def test_run_timer_updates_parent_backup_when_pending_old():
    """Timer must call _update_parent_backup so the table reflects the failure."""
    from k8si.operator.main import run_reconcile_timer

    body, status = _body_with_labels("Pending", "my-backup", created_ago_min=6)
    with patch("k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock), \
         patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock) as mock_update:
        run_coro(
            run_reconcile_timer(
                body=body, name="stuck", namespace="ns", status=status, logger=logging.getLogger()
            )
        )
    mock_update.assert_awaited_once()
    call_kwargs = mock_update.call_args
    assert call_kwargs[0][4] == "failed"  # result positional arg


def test_run_timer_updates_parent_backup_when_running_old():
    """Timer must call _update_parent_backup so the table reflects the failure."""
    from k8si.operator.main import run_reconcile_timer

    body, status = _body_with_labels("Running", "my-backup", start_ago_min=65)
    with patch("k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock), \
         patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock) as mock_update:
        run_coro(
            run_reconcile_timer(
                body=body, name="stuck", namespace="ns", status=status, logger=logging.getLogger()
            )
        )
    mock_update.assert_awaited_once()
    call_kwargs = mock_update.call_args
    assert call_kwargs[0][4] == "failed"
