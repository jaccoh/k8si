"""Tests for the K8siBackupRun reconciliation timer in k8si/operator/main.py."""

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

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
    with patch("k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
        # first call: re-read (returns empty status → proceed to kill)
        # second call: _patch_run_status; third: delete_job
        mock_thread.side_effect = [{"status": {}}, None, None]
        run_coro(
            run_reconcile_timer(
                body=body,
                name="stuck-run",
                namespace="ns",
                status=status,
                logger=logging.getLogger(),
            )
        )
    mock_thread.assert_awaited()
    patch_kwargs = mock_thread.call_args_list[1][0][3]  # index 1: _patch_run_status
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
    with patch("k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
        # job read (no conditions) → _patch_run_status → delete_job
        mock_thread.side_effect = [_mock_job(), None, None]
        run_coro(
            run_reconcile_timer(
                body=body,
                name="stuck-run",
                namespace="ns",
                status=status,
                logger=logging.getLogger(),
            )
        )
    mock_thread.assert_awaited()
    patch_kwargs = mock_thread.call_args_list[1][0][3]  # index 1: _patch_run_status
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
        mock_thread.side_effect = [{"status": {}}, None, None]
        run_coro(
            run_reconcile_timer(
                body=body, name="stuck", namespace="ns", status=status, logger=logging.getLogger()
            )
        )
    patch_kwargs = mock_thread.call_args_list[1][0][3]  # index 1: _patch_run_status
    assert "completionTime" in patch_kwargs, "completionTime must be set on timer-killed runs"


def test_run_timer_sets_completion_time_when_running_old():
    """Timer must include completionTime when marking a stuck Running run Failed."""
    from k8si.operator.main import run_reconcile_timer

    body, status = _body("Running", start_ago_min=65)
    with patch("k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
        mock_thread.side_effect = [
            _mock_job(),
            None,
            None,
        ]  # job read, _patch_run_status, delete_job
        run_coro(
            run_reconcile_timer(
                body=body, name="stuck", namespace="ns", status=status, logger=logging.getLogger()
            )
        )
    patch_kwargs = mock_thread.call_args_list[1][0][3]  # index 1: _patch_run_status
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
    with (
        patch("k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock) as mock_thread,
        patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock) as mock_update,
    ):
        # re-read run, _patch_run_status, delete_job, fetch parent backup
        mock_thread.side_effect = [
            {"status": {}},
            None,
            None,
            {"spec": {}, "metadata": {"name": "my-backup"}},
        ]
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
    with (
        patch("k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock),
        patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock) as mock_update,
    ):
        run_coro(
            run_reconcile_timer(
                body=body, name="stuck", namespace="ns", status=status, logger=logging.getLogger()
            )
        )
    mock_update.assert_awaited_once()
    call_kwargs = mock_update.call_args
    assert call_kwargs[0][4] == "failed"


# ── log entries guard: don't kill a run that already started ──────────────────


def test_run_timer_skips_pending_with_log_entries():
    """Timer must not kill a Pending run that has log entries — workflow already started.

    This guards against the race where _patch_run_status(phase=Running) fails silently
    and the run stays Pending even though the backup workflow is actively executing.
    """
    from k8si.operator.main import run_reconcile_timer

    body, status = _body("Pending", created_ago_min=6)
    status["log"] = [
        {"time": "2026-06-16T13:37:00+00:00", "phase": "QuiesceStarted", "message": ""}
    ]  # noqa: E501

    with patch("k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
        run_coro(
            run_reconcile_timer(
                body=body, name="r", namespace="ns", status=status, logger=logging.getLogger()
            )
        )
    mock_thread.assert_not_called()


def test_run_timer_still_kills_pending_with_no_log_entries():
    """Timer must still kill Pending runs with no log entries (truly stuck)."""
    from k8si.operator.main import run_reconcile_timer

    body, status = _body("Pending", created_ago_min=6)

    with patch("k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
        mock_thread.side_effect = [{"status": {}}, None, None]  # re-read: no log → kill
        run_coro(
            run_reconcile_timer(
                body=body, name="stuck", namespace="ns", status=status, logger=logging.getLogger()
            )
        )
    mock_thread.assert_awaited()
    patch_fields = mock_thread.call_args_list[1][0][3]  # index 1: _patch_run_status
    assert patch_fields["phase"] == "Failed"


# ── stale-cache guard: re-read from API before killing ───────────────────────


def test_run_timer_skips_pending_when_api_shows_log_entries():
    """Timer must re-read the run from the API before killing a stuck Pending run.

    The Kopf cache can be stale after an operator restart.  If the cache shows
    empty log but the live API shows log entries the backup is actively running —
    the timer must NOT kill it.
    """
    from k8si.operator.main import run_reconcile_timer

    body, status = _body("Pending", created_ago_min=6)
    # Cache is empty (stale after restart), but the live resource has log entries.
    live_run = {
        "status": {
            "phase": "Pending",
            "log": [{"time": "t", "phase": "QuiesceStarted", "message": ""}],
        }
    }

    with patch("k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
        mock_thread.return_value = live_run  # re-read returns run with log entries
        run_coro(
            run_reconcile_timer(
                body=body,
                name="active-run",
                namespace="ns",
                status=status,
                logger=logging.getLogger(),
            )
        )
    # Only the re-read to_thread call should have been made; no kill
    reread_calls = [
        c
        for c in mock_thread.call_args_list
        if getattr(c[0][0], "__name__", "") == "get_namespaced_custom_object"
    ]
    assert reread_calls, "Timer must re-read the run from the API before deciding to kill"
    kill_calls = [
        c
        for c in mock_thread.call_args_list
        if getattr(c[0][0], "__name__", "") == "patch_namespaced_custom_object_status"
    ]
    assert not kill_calls, "Timer must not kill a run that is actively executing (API shows log)"


def test_run_timer_skips_pending_when_api_shows_already_terminal():
    """If the live API shows the run already reached a terminal phase, timer skips."""
    from k8si.operator.main import run_reconcile_timer

    body, status = _body("Pending", created_ago_min=6)
    live_run = {"status": {"phase": "Succeeded"}}

    with patch("k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
        mock_thread.return_value = live_run
        run_coro(
            run_reconcile_timer(
                body=body,
                name="done-run",
                namespace="ns",
                status=status,
                logger=logging.getLogger(),
            )
        )
    kill_calls = [
        c
        for c in mock_thread.call_args_list
        if getattr(c[0][0], "__name__", "") == "patch_namespaced_custom_object_status"
    ]
    assert not kill_calls, "Timer must not kill a run that is already terminal in the API"


# ── Running: job-status reconciliation ───────────────────────────────────────


def _mock_job(complete: bool = False, failed: bool = False) -> MagicMock:
    """Return a MagicMock V1Job with the appropriate conditions."""
    job = MagicMock()
    conditions = []
    if complete:
        c = MagicMock()
        c.type = "Complete"
        c.status = "True"
        conditions.append(c)
    if failed:
        c = MagicMock()
        c.type = "Failed"
        c.status = "True"
        conditions.append(c)
    job.status.conditions = conditions
    return job


def test_run_timer_reconciles_running_to_succeeded_when_job_complete():
    """If the K8s Job completed but run is still Running, timer must patch to Succeeded.

    This handles the case where the operator restarted between job completion and
    _patch_run_status — the backup is done, but the run is stuck in Running.
    """
    from k8si.operator.main import run_reconcile_timer

    body, status = _body_with_labels("Running", "my-backup", start_ago_min=10)

    with (
        patch("k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock) as mock_thread,
        patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock) as mock_update,
    ):
        mock_thread.side_effect = [
            _mock_job(complete=True),  # read_namespaced_job: job is done
            None,  # _patch_run_status → Succeeded
            {"spec": {}, "metadata": {"name": "my-backup"}},  # get parent backup
        ]
        run_coro(
            run_reconcile_timer(
                body=body,
                name="my-run",
                namespace="ns",
                status=status,
                logger=logging.getLogger(),
            )
        )

    status_calls = [
        c
        for c in mock_thread.call_args_list
        if getattr(c[0][0], "__name__", "") == "_patch_run_status"
    ]
    assert status_calls, "Timer must call _patch_run_status when job completed"
    patch_fields = status_calls[0][0][3]
    assert patch_fields["phase"] == "Succeeded", (
        f"Expected Succeeded (job done), got {patch_fields['phase']!r}"
    )
    assert "completionTime" in patch_fields

    mock_update.assert_awaited_once()
    assert mock_update.call_args[0][4] == "success"  # result arg


def test_run_timer_reconciles_running_to_failed_when_job_failed():
    """If the K8s Job is Failed, timer marks run Failed immediately — not after 60 min."""
    from k8si.operator.main import run_reconcile_timer

    body, status = _body_with_labels("Running", "my-backup", start_ago_min=10)  # only 10 min!

    with (
        patch("k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock) as mock_thread,
        patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock),
    ):
        mock_thread.side_effect = [
            _mock_job(failed=True),  # read_namespaced_job
            None,  # _patch_run_status → Failed
            None,  # delete_namespaced_job
            {"spec": {}, "metadata": {"name": "my-backup"}},  # get parent backup
        ]
        run_coro(
            run_reconcile_timer(
                body=body,
                name="my-run",
                namespace="ns",
                status=status,
                logger=logging.getLogger(),
            )
        )

    status_calls = [
        c
        for c in mock_thread.call_args_list
        if getattr(c[0][0], "__name__", "") == "_patch_run_status"
    ]
    assert status_calls, "Timer must patch run to Failed when job failed"
    patch_fields = status_calls[0][0][3]
    assert patch_fields["phase"] == "Failed"
    assert "job failed" in patch_fields.get("message", "").lower()


# ── orphaned Job cleanup ──────────────────────────────────────────────────────


def test_run_timer_deletes_orphaned_job_when_killing_pending():
    """Timer must delete the K8s Job (same name as run) when killing a stuck Pending run.

    The Job deletion is dispatched via asyncio.to_thread; check the call list for a
    call whose first argument is a delete_namespaced_job bound method.
    """
    from k8si.operator.main import run_reconcile_timer

    body, status = _body("Pending", created_ago_min=6)
    with patch("k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
        mock_thread.side_effect = [{"status": {}}, None, None]
        run_coro(
            run_reconcile_timer(
                body=body,
                name="stuck-run",
                namespace="ns",
                status=status,
                logger=logging.getLogger(),
            )
        )
    delete_calls = [
        c
        for c in mock_thread.call_args_list
        if getattr(c[0][0], "__name__", "") == "delete_namespaced_job"
    ]
    assert delete_calls, "Timer must call delete_namespaced_job for orphaned Job"
    assert delete_calls[0][0][1] == "stuck-run"
    assert delete_calls[0][0][2] == "ns"
