"""K8siBackupRun retention — older runs are pruned when one reaches a terminal phase.

Every K8siBackupRun carries a 60s kopf timer for the operator's whole life;
without pruning they accumulate forever and each tick is wasted API traffic.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

from tests.helpers import SPEC, run_coro


def _run_item(name: str, created: str = "2026-08-30T00:00:00Z") -> dict:
    return {"metadata": {"name": name, "creationTimestamp": created, "labels": {}}}


def _custom_with_runs(runs: list[dict]) -> MagicMock:
    custom = MagicMock()
    custom.list_namespaced_custom_object.return_value = {"items": runs}
    return custom


def _deleted_names(custom: MagicMock) -> list[str]:
    return [c.args[4] for c in custom.delete_namespaced_custom_object.call_args_list]


def _make_runs(count: int) -> list[dict]:
    """b-0000 (oldest) .. b-NNNN (newest); creationTimestamp follows the name."""
    return [_run_item(f"b-{i:04d}", f"2026-08-{i + 1:02d}T00:00:00Z") for i in range(count)]


# ── _prune_old_runs unit behaviour ────────────────────────────────────────────


def test_prune_deletes_runs_beyond_retention():
    """Only the newest `keep` runs survive; the oldest are deleted."""
    from k8si.operator.main import _prune_old_runs

    custom = _custom_with_runs(_make_runs(35))

    async def _run():
        await _prune_old_runs(custom, "default", "b", keep=30)

    run_coro(_run())

    deleted = _deleted_names(custom)
    assert len(deleted) == 5
    assert deleted == ["b-0000", "b-0001", "b-0002", "b-0003", "b-0004"], (
        f"oldest must go first, got {deleted}"
    )


def test_prune_noop_at_or_below_retention():
    from k8si.operator.main import _prune_old_runs

    custom = _custom_with_runs(_make_runs(30))

    async def _run():
        await _prune_old_runs(custom, "default", "b", keep=30)

    run_coro(_run())
    custom.delete_namespaced_custom_object.assert_not_called()


def test_prune_scopes_to_backup_via_label_selector():
    from k8si.operator.main import _prune_old_runs

    custom = _custom_with_runs(_make_runs(32))

    async def _run():
        await _prune_old_runs(custom, "default", "b", keep=30)

    run_coro(_run())

    custom.list_namespaced_custom_object.assert_called_once()
    assert (
        custom.list_namespaced_custom_object.call_args.kwargs["label_selector"]
        == "k8si.io/backup=b"
    )


def test_prune_retention_default_is_30():
    from k8si.operator.main import _RUN_RETENTION_DEFAULT, _prune_old_runs

    custom = _custom_with_runs(_make_runs(31))

    async def _run():
        await _prune_old_runs(custom, "default", "b")  # keep=None → default

    run_coro(_run())
    assert _RUN_RETENTION_DEFAULT == 30
    assert _deleted_names(custom) == ["b-0000"]


def test_prune_retention_env_override(monkeypatch):
    """K8SI_RUN_RETENTION overrides the default keep count."""
    from k8si.operator.main import _prune_old_runs, _resolve_run_retention

    monkeypatch.setenv("K8SI_RUN_RETENTION", "3")
    assert _resolve_run_retention() == 3

    custom = _custom_with_runs(_make_runs(5))

    async def _run():
        await _prune_old_runs(custom, "default", "b")

    run_coro(_run())
    assert _deleted_names(custom) == ["b-0000", "b-0001"]


def test_prune_retention_env_invalid_falls_back(monkeypatch):
    from k8si.operator.main import _RUN_RETENTION_DEFAULT, _resolve_run_retention

    monkeypatch.setenv("K8SI_RUN_RETENTION", "not-a-number")
    assert _resolve_run_retention() == _RUN_RETENTION_DEFAULT


def test_prune_swallows_list_failure():
    """Best-effort: a listing failure must never fail the backup."""
    from k8si.operator.main import _prune_old_runs

    custom = MagicMock()
    custom.list_namespaced_custom_object.side_effect = RuntimeError("api down")

    async def _run():
        await _prune_old_runs(custom, "default", "b", keep=30)  # must not raise

    run_coro(_run())


def test_prune_swallows_delete_failure():
    from k8si.operator.main import _prune_old_runs

    custom = _custom_with_runs(_make_runs(32))
    custom.delete_namespaced_custom_object.side_effect = RuntimeError("api down")

    async def _run():
        await _prune_old_runs(custom, "default", "b", keep=30)  # must not raise

    run_coro(_run())
    assert custom.delete_namespaced_custom_object.call_count == 2


def test_prune_ignores_404_on_delete():
    """A run already gone (e.g. deleted by garbage collection) is not an error."""
    import kubernetes.client.exceptions

    from k8si.operator.main import _prune_old_runs

    custom = _custom_with_runs(_make_runs(32))
    gone = kubernetes.client.exceptions.ApiException(status=404)
    gone.status = 404
    custom.delete_namespaced_custom_object.side_effect = [gone, None]

    async def _run():
        await _prune_old_runs(custom, "default", "b", keep=30)  # must not raise

    run_coro(_run())
    assert custom.delete_namespaced_custom_object.call_count == 2


# ── wiring: on_run_create prunes when the run reaches terminal phase ──────────


def _backup_obj() -> dict:
    return {"metadata": {"name": "test", "namespace": "default"}, "spec": SPEC, "status": {}}


def _run_spec() -> dict:
    return {"backupRef": "test", "triggeredBy": "schedule", "triggeredAt": "2026-08-30T10:00:00Z"}


def test_on_run_create_prunes_after_success():
    from k8si.operator.main import on_run_create

    async def _run():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main._patch_run_status"),
            patch(
                "k8si.operator.main.workflow.run_backup", new_callable=AsyncMock
            ) as mock_run_backup,
            patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock),
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main._prune_old_runs", new_callable=AsyncMock) as mock_prune,
        ):
            mock_k8s = MagicMock()
            mock_k8s.get_namespaced_custom_object.return_value = _backup_obj()
            mock_k8s_cls.return_value = mock_k8s
            mock_run_backup.return_value = {}

            await on_run_create(
                body={},
                spec=_run_spec(),
                name="test-run",
                namespace="default",
                logger=logging.getLogger("test"),
            )

            mock_prune.assert_awaited_once()

    run_coro(_run())


def test_on_run_create_prunes_after_failure():
    from k8si.operator.main import on_run_create

    async def _run():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main._patch_run_status"),
            patch(
                "k8si.operator.main.workflow.run_backup",
                new_callable=AsyncMock,
                side_effect=RuntimeError("boom"),
            ),
            patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock),
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main._prune_old_runs", new_callable=AsyncMock) as mock_prune,
        ):
            mock_k8s = MagicMock()
            mock_k8s.get_namespaced_custom_object.return_value = _backup_obj()
            mock_k8s_cls.return_value = mock_k8s

            await on_run_create(
                body={},
                spec=_run_spec(),
                name="test-run",
                namespace="default",
                logger=logging.getLogger("test"),
            )

            mock_prune.assert_awaited_once()

    run_coro(_run())


# ── wiring: run_reconcile_timer prunes on terminal handling ───────────────────


def _mock_job(complete: bool = False) -> MagicMock:
    job = MagicMock()
    conditions = []
    if complete:
        c = MagicMock()
        c.type = "Complete"
        c.status = "True"
        conditions.append(c)
    job.status.conditions = conditions
    return job


def test_run_timer_prunes_after_reconciling_to_succeeded():
    """Reconciler terminal path (job complete, run still Running) must prune."""
    from datetime import UTC, datetime, timedelta

    from k8si.operator.main import run_reconcile_timer

    start = (datetime.now(tz=UTC) - timedelta(minutes=10)).isoformat()
    body = {
        "metadata": {
            "creationTimestamp": start,
            "labels": {"k8si.io/backup": "my-backup"},
        }
    }
    status = {"phase": "Running", "startTime": start}

    with (
        patch("k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock) as mock_thread,
        patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock),
        patch("k8si.operator.main._prune_old_runs", new_callable=AsyncMock) as mock_prune,
    ):
        mock_thread.side_effect = [
            _mock_job(complete=True),  # read_namespaced_job
            None,  # _patch_run_status → Succeeded
            {"spec": {}, "metadata": {"name": "my-backup"}},  # parent backup fetch
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

    mock_prune.assert_awaited_once()
    assert mock_prune.call_args.args[1:] == ("ns", "my-backup")


def test_run_timer_prunes_after_marking_failed():
    from datetime import UTC, datetime, timedelta

    from k8si.operator.main import run_reconcile_timer

    created = (datetime.now(tz=UTC) - timedelta(minutes=6)).isoformat()
    body = {
        "metadata": {
            "creationTimestamp": created,
            "labels": {"k8si.io/backup": "my-backup"},
        }
    }
    status = {"phase": "Pending"}

    with (
        patch("k8si.operator.main.asyncio.to_thread", new_callable=AsyncMock) as mock_thread,
        patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock),
        patch("k8si.operator.main._prune_old_runs", new_callable=AsyncMock) as mock_prune,
    ):
        mock_thread.side_effect = [
            {"status": {}},  # re-read: no log → proceed to kill
            None,  # _patch_run_status → Failed
            None,  # delete_namespaced_job
            {"spec": {}, "metadata": {"name": "my-backup"}},  # parent backup fetch
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

    mock_prune.assert_awaited_once()
    assert mock_prune.call_args.args[1:] == ("ns", "my-backup")
