"""Race safety of _update_parent_backup in k8si/operator/main.py.

The histories (recentRuns/recentBackups) are read-modify-write. The backup_obj
handed in by callers was fetched before the backup ran — potentially hours
earlier — so a concurrently finished run's entries would be silently dropped by
a patch built from that stale snapshot.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import kubernetes.client.exceptions

from tests.helpers import SPEC, run_coro

_RUN_RESULT = {"lastBackupTime": "2026-08-30T02:00:00+00:00", "message": ""}
_CONCURRENT_TIME = "2026-08-29T02:00:00+00:00"


def _live_backup_obj(status: dict) -> dict:
    return {"metadata": {"name": "test", "namespace": "default"}, "spec": SPEC, "status": status}


def _patched_status(custom: MagicMock) -> dict:
    call = custom.patch_namespaced_custom_object_status.call_args
    body = call.args[5] if len(call.args) > 5 else call.kwargs.get("body", {})
    return body["status"]


# ── re-read before patch ──────────────────────────────────────────────────────


def test_update_parent_rereads_backup_before_patch():
    """The patch must be built from a freshly re-read object, not the caller's
    stale backup_obj — otherwise a concurrent run's history entries vanish."""
    from k8si.operator.main import _update_parent_backup

    custom = MagicMock()
    stale_obj = _live_backup_obj({})
    custom.get_namespaced_custom_object.return_value = _live_backup_obj(
        {
            "recentBackups": [{"time": _CONCURRENT_TIME, "result": "success"}],
            "recentRuns": [
                {"name": "concurrent-run", "time": _CONCURRENT_TIME, "result": "success"}
            ],
        }
    )

    async def _run():
        with (
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main._notify_webhook", new_callable=AsyncMock),
        ):
            await _update_parent_backup(
                custom, "test", "default", "this-run", "success", _RUN_RESULT, stale_obj, SPEC, 10
            )

    run_coro(_run())

    custom.get_namespaced_custom_object.assert_called_once()
    status = _patched_status(custom)
    times = [e["time"] for e in status["recentBackups"]]
    assert _CONCURRENT_TIME in times, "concurrent run's recentBackups entry was dropped"
    names = [e["name"] for e in status["recentRuns"]]
    assert "concurrent-run" in names, "concurrent run's recentRuns entry was dropped"


def test_update_parent_falls_back_to_caller_snapshot_when_reread_fails():
    """If the re-read fails, the caller's backup_obj is still better than nothing."""
    from k8si.operator.main import _update_parent_backup

    custom = MagicMock()
    custom.get_namespaced_custom_object.side_effect = RuntimeError("api down")
    stale_obj = _live_backup_obj(
        {"recentBackups": [{"time": _CONCURRENT_TIME, "result": "success"}]}
    )

    async def _run():
        with (
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main._notify_webhook", new_callable=AsyncMock),
        ):
            await _update_parent_backup(
                custom, "test", "default", "this-run", "success", _RUN_RESULT, stale_obj, SPEC, 10
            )

    run_coro(_run())

    status = _patched_status(custom)
    times = [e["time"] for e in status["recentBackups"]]
    assert _CONCURRENT_TIME in times


# ── 409 conflict retry ────────────────────────────────────────────────────────


def _conflict() -> kubernetes.client.exceptions.ApiException:
    exc = kubernetes.client.exceptions.ApiException(status=409)
    exc.status = 409
    return exc


def test_update_parent_retries_on_409_conflict():
    """A 409 from the status patch must re-read and retry, not drop the update."""
    from k8si.operator.main import _update_parent_backup

    custom = MagicMock()
    custom.get_namespaced_custom_object.return_value = _live_backup_obj({})
    custom.patch_namespaced_custom_object_status.side_effect = [_conflict(), None]
    mock_sleep = AsyncMock()

    async def _run():
        with (
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main._notify_webhook", new_callable=AsyncMock),
            patch("asyncio.sleep", mock_sleep),
        ):
            await _update_parent_backup(
                custom,
                "test",
                "default",
                "this-run",
                "success",
                _RUN_RESULT,
                _live_backup_obj({}),
                SPEC,
                10,
            )

    run_coro(_run())

    assert custom.patch_namespaced_custom_object_status.call_count == 2
    mock_sleep.assert_awaited()  # small backoff between attempts


def test_update_parent_retries_pick_up_concurrent_entry():
    """After a 409, the retry must re-read and merge the entry the winner wrote."""
    from k8si.operator.main import _update_parent_backup

    custom = MagicMock()
    # Re-read after the conflict sees the concurrent run that beat us to the patch.
    custom.get_namespaced_custom_object.side_effect = [
        _live_backup_obj({}),  # initial re-read
        _live_backup_obj(
            {"recentRuns": [{"name": "winner", "time": _CONCURRENT_TIME, "result": "success"}]}
        ),
    ]
    custom.patch_namespaced_custom_object_status.side_effect = [_conflict(), None]

    async def _run():
        with (
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main._notify_webhook", new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await _update_parent_backup(
                custom,
                "test",
                "default",
                "this-run",
                "success",
                _RUN_RESULT,
                _live_backup_obj({}),
                SPEC,
                10,
            )

    run_coro(_run())

    names = [e["name"] for e in _patched_status(custom)["recentRuns"]]
    assert "winner" in names, "retry must merge the concurrent winner's entry"


def test_update_parent_gives_up_after_repeated_409_without_raising():
    """After 3 failed attempts the update is dropped with a log line — it must
    never raise into the run handler, and metrics/webhook still fire once."""
    from k8si.operator.main import _update_parent_backup

    custom = MagicMock()
    custom.get_namespaced_custom_object.return_value = _live_backup_obj({})
    custom.patch_namespaced_custom_object_status.side_effect = [
        _conflict(),
        _conflict(),
        _conflict(),
    ]
    mock_metrics = MagicMock()

    async def _run():
        with (
            patch("k8si.operator.main.metrics.record", mock_metrics),
            patch("k8si.operator.main._notify_webhook", new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await _update_parent_backup(
                custom,
                "test",
                "default",
                "this-run",
                "success",
                _RUN_RESULT,
                _live_backup_obj({}),
                SPEC,
                10,
            )

    run_coro(_run())  # must not raise

    assert custom.patch_namespaced_custom_object_status.call_count == 3
    assert mock_metrics.call_count == 1


def test_update_parent_non_409_error_does_not_retry():
    """Only 409 conflicts retry — other API errors surface immediately (logged)."""
    from k8si.operator.main import _update_parent_backup

    custom = MagicMock()
    custom.get_namespaced_custom_object.return_value = _live_backup_obj({})
    forbidden = kubernetes.client.exceptions.ApiException(status=403)
    forbidden.status = 403
    custom.patch_namespaced_custom_object_status.side_effect = forbidden
    mock_sleep = AsyncMock()

    async def _run():
        with (
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main._notify_webhook", new_callable=AsyncMock),
            patch("asyncio.sleep", mock_sleep),
        ):
            await _update_parent_backup(
                custom,
                "test",
                "default",
                "this-run",
                "success",
                _RUN_RESULT,
                _live_backup_obj({}),
                SPEC,
                10,
            )

    run_coro(_run())

    assert custom.patch_namespaced_custom_object_status.call_count == 1
    mock_sleep.assert_not_awaited()
