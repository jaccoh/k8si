"""Tests for the K8siBackupRun reconciler (on_run_create) in k8si/operator/main.py."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

from tests.helpers import SPEC, run_coro

_BACKUP_OBJ = {
    "metadata": {"name": "test", "namespace": "default"},
    "spec": SPEC,
    "status": {},
}


def _run_spec(triggered_by: str = "schedule") -> dict:
    return {
        "backupRef": "test",
        "triggeredBy": triggered_by,
        "triggeredAt": "2026-06-14T10:00:00+00:00",
        "mode": "snapshot",
    }


# ── backfill guard ────────────────────────────────────────────────────────────


def test_on_run_create_skips_backfill():
    """Runs with triggeredBy=backfill are skipped without calling run_backup."""
    from k8si.operator.main import on_run_create

    async def _run():
        with patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run:
            await on_run_create(
                body={},
                spec=_run_spec("backfill"),
                name="test-backfill",
                namespace="default",
                logger=logging.getLogger("test"),
            )
            mock_run.assert_not_called()

    run_coro(_run())


# ── parent lookup failure ─────────────────────────────────────────────────────


def test_on_run_create_patches_failed_when_parent_missing():
    """If the parent K8siBackup doesn't exist, phase is set to Failed."""
    from k8si.operator.main import on_run_create

    async def _run():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main._patch_run_status") as mock_patch,
        ):
            mock_k8s = MagicMock()
            mock_k8s.get_namespaced_custom_object.side_effect = RuntimeError("not found")
            mock_k8s_cls.return_value = mock_k8s

            await on_run_create(
                body={},
                spec=_run_spec(),
                name="test-run",
                namespace="default",
                logger=logging.getLogger("test"),
            )

            failed_call = next(
                (c for c in mock_patch.call_args_list if c.args[2].get("phase") == "Failed"), None
            )
            assert failed_call is not None

    run_coro(_run())


# ── success path ──────────────────────────────────────────────────────────────


def test_on_run_create_sets_succeeded_on_success():
    """Successful run_backup results in phase=Succeeded on the run and updates parent."""
    from k8si.operator.main import on_run_create

    async def _run():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main._patch_run_status") as mock_patch,
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock),
            patch("k8si.operator.main.metrics.record"),
        ):
            mock_k8s = MagicMock()
            mock_k8s.get_namespaced_custom_object.return_value = _BACKUP_OBJ
            mock_k8s_cls.return_value = mock_k8s
            mock_run.return_value = {"lastBackupTime": "2026-06-14T10:30:00+00:00", "message": ""}

            await on_run_create(
                body={},
                spec=_run_spec(),
                name="test-run",
                namespace="default",
                logger=logging.getLogger("test"),
            )

            phases = [c.args[2].get("phase") for c in mock_patch.call_args_list if c.args[2]]
            # "Queued" up front — "Running" only arrives via the on_job_created
            # callback once the Job actually starts (the mock never invokes it).
            assert "Queued" in phases
            assert "Succeeded" in phases

    run_coro(_run())


# ── failure path ──────────────────────────────────────────────────────────────


def test_on_run_create_sets_failed_on_exception():
    """If run_backup raises, phase is set to Failed and message is propagated."""
    from k8si.operator.main import on_run_create

    async def _run():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main._patch_run_status") as mock_patch,
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock),
            patch("k8si.operator.main.metrics.record"),
        ):
            mock_k8s = MagicMock()
            mock_k8s.get_namespaced_custom_object.return_value = _BACKUP_OBJ
            mock_k8s_cls.return_value = mock_k8s
            mock_run.side_effect = RuntimeError("disk full")

            await on_run_create(
                body={},
                spec=_run_spec(),
                name="test-run",
                namespace="default",
                logger=logging.getLogger("test"),
            )

            failed_call = next(
                (c for c in mock_patch.call_args_list if c.args[2].get("phase") == "Failed"), None
            )
            assert failed_call is not None
            assert "disk full" in failed_call.args[2].get("message", "")

    run_coro(_run())


# ── queued → running flip at Job start ────────────────────────────────────────


def test_on_run_create_marks_queued_not_running():
    """The run is marked Queued (not Running) before run_backup starts: with
    the concurrency semaphore (#6) the handler can park for a long time before
    any Job exists, and during that wait neither the run nor the parent badge
    may claim "running"."""
    from k8si.operator.main import on_run_create

    async def _run():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main._patch_run_status") as mock_patch,
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock),
            patch("k8si.operator.main.metrics.record"),
        ):
            mock_k8s = MagicMock()
            mock_k8s.get_namespaced_custom_object.return_value = _BACKUP_OBJ
            mock_k8s_cls.return_value = mock_k8s
            mock_run.return_value = {}

            await on_run_create(
                body={},
                spec=_run_spec(),
                name="test-run",
                namespace="default",
                logger=logging.getLogger("test"),
            )

            phases = [c.args[2].get("phase") for c in mock_patch.call_args_list if c.args[2]]
            assert "Queued" in phases
            assert "Running" not in phases, (
                "Running must only be set by the on_job_created callback at Job start"
            )
            assert mock_run.call_args.kwargs.get("on_job_created") is not None, (
                "run_backup must receive the on_job_created callback"
            )

    run_coro(_run())


def test_on_job_created_flips_run_and_parent_to_running():
    """Invoking the callback handed to run_backup (at Job start) must:
    - patch the run to phase=Running with startTime AND jobName together (the
      reconciler looks the Job up via status.jobName, #5),
    - flip the parent K8siBackup lastBackupResult from 'queued' to 'running',
    - record the running metric."""
    from k8si.operator.main import on_run_create

    async def _run():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main._patch_run_status") as mock_patch,
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock),
            patch("k8si.operator.main.metrics.record") as mock_metrics,
            patch("k8si.operator.main.kopf.event"),
        ):
            mock_k8s = MagicMock()
            mock_k8s.get_namespaced_custom_object.return_value = _BACKUP_OBJ
            mock_k8s_cls.return_value = mock_k8s

            async def _capture(*args, **kwargs):
                await kwargs["on_job_created"]("k8si-test-20260830")

            mock_run.side_effect = _capture

            await on_run_create(
                body={},
                spec=_run_spec(),
                name="test-run",
                namespace="default",
                logger=logging.getLogger("test"),
            )

            running_call = next(
                (c for c in mock_patch.call_args_list if c.args[2].get("phase") == "Running"), None
            )
            assert running_call is not None, "callback must patch run phase to Running"
            fields = running_call.args[2]
            assert fields.get("jobName") == "k8si-test-20260830"
            assert fields.get("startTime"), "startTime must be recorded at Job start"

            parent_call = next(
                (
                    c
                    for c in mock_k8s.patch_namespaced_custom_object_status.call_args_list
                    if c.args[3] == "k8sibackups"
                ),
                None,
            )
            assert parent_call is not None, "callback must patch the parent backup status"
            assert parent_call.args[5]["status"]["lastBackupResult"] == "running"

            assert any(c.args[2] == "running" for c in mock_metrics.call_args_list), (
                "metrics must record the queued→running flip"
            )

    run_coro(_run())


# ── _running guard ────────────────────────────────────────────────────────────


def test_on_run_create_discards_running_key_on_completion():
    """on_run_create adds (ns, backup_name) to _running and discards it on completion."""
    import k8si.operator.main as main_module
    from k8si.operator.main import on_run_create

    key = ("default", "test")

    async def _run():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main._patch_run_status"),
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock),
            patch("k8si.operator.main.metrics.record"),
        ):
            mock_k8s = MagicMock()
            mock_k8s.get_namespaced_custom_object.return_value = _BACKUP_OBJ
            mock_k8s_cls.return_value = mock_k8s
            mock_run.return_value = {}

            await on_run_create(
                body={},
                spec=_run_spec(),
                name="test-run",
                namespace="default",
                logger=logging.getLogger("test"),
            )

    run_coro(_run())

    assert key not in main_module._running


def test_on_run_create_rejects_concurrent_run():
    """A second on_run_create for the same backup is marked Failed if key is in _running."""
    import k8si.operator.main as main_module
    from k8si.operator.main import on_run_create

    key = ("default", "test")
    main_module._running.add(key)  # simulate first run already in progress

    async def _run():
        with patch("k8si.operator.main._patch_run_status") as mock_patch:
            await on_run_create(
                body={},
                spec=_run_spec(),
                name="test-run-duplicate",
                namespace="default",
                logger=logging.getLogger("test"),
            )
            failed_call = next(
                (c for c in mock_patch.call_args_list if c.args[2].get("phase") == "Failed"), None
            )
            assert failed_call is not None
            assert "concurrent" in failed_call.args[2].get("message", "")

    run_coro(_run())
    assert key in main_module._running  # first run's key untouched


def test_on_run_create_adds_key_to_running():
    """on_run_create claims _running before starting the backup, preventing duplicates."""
    import k8si.operator.main as main_module
    from k8si.operator.main import on_run_create

    captured_during = []

    async def fake_run_backup(*args, **kwargs):
        captured_during.append(("default", "test") in main_module._running)
        return {}

    async def _run():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main._patch_run_status"),
            patch("k8si.operator.main.workflow.run_backup", side_effect=fake_run_backup),
            patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock),
            patch("k8si.operator.main.metrics.record"),
        ):
            mock_k8s = MagicMock()
            mock_k8s.get_namespaced_custom_object.return_value = _BACKUP_OBJ
            mock_k8s_cls.return_value = mock_k8s
            await on_run_create(
                body={},
                spec=_run_spec(),
                name="test-run",
                namespace="default",
                logger=logging.getLogger("test"),
            )

    run_coro(_run())
    assert captured_during == [True], "key must be in _running while backup executes"


# ── completionTime on early failure paths ─────────────────────────────────────


def test_on_run_create_sets_completion_time_when_concurrent_rejected():
    """Concurrent run rejection must include completionTime in the Failed patch."""
    import k8si.operator.main as main_module
    from k8si.operator.main import on_run_create

    key = ("default", "test")
    main_module._running.add(key)

    async def _run():
        with patch("k8si.operator.main._patch_run_status") as mock_patch:
            await on_run_create(
                body={},
                spec=_run_spec(),
                name="test-run-dup",
                namespace="default",
                logger=logging.getLogger("test"),
            )
        failed_call = next(
            (c for c in mock_patch.call_args_list if c.args[2].get("phase") == "Failed"), None
        )
        assert failed_call is not None
        assert "completionTime" in failed_call.args[2], (
            "completionTime must be set on concurrent rejection"
        )

    run_coro(_run())
    assert key in main_module._running  # first run's key untouched


def test_on_run_create_sets_completion_time_when_parent_missing():
    """Parent backup lookup failure must include completionTime in the Failed patch."""
    from k8si.operator.main import on_run_create

    async def _run():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main._patch_run_status") as mock_patch,
        ):
            mock_k8s = MagicMock()
            mock_k8s.get_namespaced_custom_object.side_effect = RuntimeError("not found")
            mock_k8s_cls.return_value = mock_k8s

            await on_run_create(
                body={},
                spec=_run_spec(),
                name="test-run",
                namespace="default",
                logger=logging.getLogger("test"),
            )

        failed_call = next(
            (c for c in mock_patch.call_args_list if c.args[2].get("phase") == "Failed"), None
        )
        assert failed_call is not None
        assert "completionTime" in failed_call.args[2], (
            "completionTime must be set on parent-fetch failure"
        )

    run_coro(_run())


# ── timer-killed guard ────────────────────────────────────────────────────────


# ── run spec mode override ────────────────────────────────────────────────────


def test_on_run_create_run_mode_overrides_parent_backup_mode():
    """K8siBackupRun.spec.mode must override parent K8siBackup.spec.backupMode."""
    from k8si.operator.main import on_run_create

    parent_direct = {
        "metadata": {"name": "test", "namespace": "default"},
        "spec": {**SPEC, "backupMode": "direct"},
        "status": {},
    }

    captured_spec = []

    async def fake_run_backup(_name, _ns, backup_spec, *args, **kwargs):
        captured_spec.append(backup_spec.get("backupMode"))
        return {}

    async def _run():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main._patch_run_status"),
            patch("k8si.operator.main.workflow.run_backup", side_effect=fake_run_backup),
            patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock),
            patch("k8si.operator.main.metrics.record"),
        ):
            mock_k8s = MagicMock()
            mock_k8s.get_namespaced_custom_object.return_value = parent_direct
            mock_k8s_cls.return_value = mock_k8s

            await on_run_create(
                body={},
                spec={**_run_spec(), "mode": "snapshot"},
                name="test-run",
                namespace="default",
                logger=logging.getLogger("test"),
            )

    run_coro(_run())
    assert captured_spec == ["snapshot"], (
        f"run_backup must see backupMode=snapshot (from run spec), got {captured_spec}"
    )


def test_on_run_create_run_mode_absent_uses_parent_backup_mode():
    """Without K8siBackupRun.spec.mode, parent K8siBackup.spec.backupMode is used unchanged."""
    from k8si.operator.main import on_run_create

    parent_direct = {
        "metadata": {"name": "test", "namespace": "default"},
        "spec": {**SPEC, "backupMode": "direct"},
        "status": {},
    }

    captured_spec = []

    async def fake_run_backup(_name, _ns, backup_spec, *args, **kwargs):
        captured_spec.append(backup_spec.get("backupMode"))
        return {}

    async def _run():
        run_spec_no_mode = {k: v for k, v in _run_spec().items() if k != "mode"}
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main._patch_run_status"),
            patch("k8si.operator.main.workflow.run_backup", side_effect=fake_run_backup),
            patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock),
            patch("k8si.operator.main.metrics.record"),
        ):
            mock_k8s = MagicMock()
            mock_k8s.get_namespaced_custom_object.return_value = parent_direct
            mock_k8s_cls.return_value = mock_k8s

            await on_run_create(
                body={},
                spec=run_spec_no_mode,
                name="test-run",
                namespace="default",
                logger=logging.getLogger("test"),
            )

    run_coro(_run())
    assert captured_spec == ["direct"], (
        f"run_backup must see backupMode=direct (from parent), got {captured_spec}"
    )


# ── _running leak guards ──────────────────────────────────────────────────────


def test_on_run_create_discards_running_key_when_patch_running_raises():
    """If _patch_run_status(Running) raises, _running must still be discarded.

    Regression guard for path-2 leak: _patch_run_status(Running) was called
    OUTSIDE the try/finally, so an API error left the key stuck in _running
    forever — backup_timer skips the backup on every subsequent tick.
    """
    import k8si.operator.main as main_module
    from k8si.operator.main import on_run_create

    key = ("default", "test")

    def patch_status_side_effect(ns, name, patch):
        if patch.get("phase") == "Running":
            raise RuntimeError("k8s API timeout")

    async def _run():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main._patch_run_status", side_effect=patch_status_side_effect),
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock),
            patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock),
            patch("k8si.operator.main.metrics.record"),
        ):
            mock_k8s = MagicMock()
            mock_k8s.get_namespaced_custom_object.return_value = _BACKUP_OBJ
            mock_k8s_cls.return_value = mock_k8s

            await on_run_create(
                body={},
                spec=_run_spec(),
                name="test-run",
                namespace="default",
                logger=logging.getLogger("test"),
            )

    run_coro(_run())
    assert key not in main_module._running, (
        "_running must be discarded even if patch(Running) raises"
    )


def test_on_run_create_discards_running_key_when_parent_missing_and_patch_fails():
    """If parent lookup fails AND _patch_run_status(Failed) also raises, key must be discarded.

    Regression guard for path-1 leak: first except block called _running.discard
    AFTER the _patch_run_status await — if that await raised, discard was skipped.
    """
    import k8si.operator.main as main_module
    from k8si.operator.main import on_run_create

    key = ("default", "test")

    async def _run():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch(
                "k8si.operator.main._patch_run_status",
                side_effect=RuntimeError("patch also failed"),
            ),
        ):
            mock_k8s = MagicMock()
            mock_k8s.get_namespaced_custom_object.side_effect = RuntimeError("not found")
            mock_k8s_cls.return_value = mock_k8s

            try:
                await on_run_create(
                    body={},
                    spec=_run_spec(),
                    name="test-run",
                    namespace="default",
                    logger=logging.getLogger("test"),
                )
            except Exception:
                pass  # exception may propagate — what matters is _running state

    run_coro(_run())
    assert key not in main_module._running, (
        "_running must be discarded even if patch(Failed) raises"
    )


def test_on_run_create_does_not_overwrite_timer_killed_run_with_succeeded():
    """If the timer killed the run (phase=Failed) while the backup was executing,
    on_run_create must not overwrite that Failed status with Succeeded."""
    from k8si.operator.main import on_run_create

    async def _run():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main._patch_run_status") as mock_patch,
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock),
            patch("k8si.operator.main.metrics.record"),
        ):
            mock_k8s = MagicMock()
            # get_namespaced_custom_object sequence:
            # 1. _run_has_live_job re-read (Pending — fresh run, no live Job)
            # 2. backup lookup
            # 3. re-read run after backup — timer already killed it.
            mock_k8s.get_namespaced_custom_object.side_effect = [
                {"status": {"phase": "Pending"}},
                _BACKUP_OBJ,
                {"status": {"phase": "Failed"}, "metadata": {"name": "test-run"}},
            ]
            mock_k8s_cls.return_value = mock_k8s

            async def _capture(*args, **kwargs):
                await kwargs["on_job_created"]("k8si-test-20260830")

            mock_run.side_effect = _capture

            await on_run_create(
                body={},
                spec=_run_spec(),
                name="test-run",
                namespace="default",
                logger=logging.getLogger("test"),
            )

        phases = [c.args[2].get("phase") for c in mock_patch.call_args_list if c.args[2]]
        assert "Succeeded" not in phases, "Must not overwrite timer-killed run with Succeeded"
        assert "Queued" in phases, "Must mark the run Queued before the backup starts"
        assert "Running" in phases, "Must flip to Running when the Job starts"

    run_coro(_run())


# ── goal #5: record jobName on the run ──────────────────────────────────────


def test_on_run_create_records_job_name_on_success():
    """run_backup returns the actual Job name (k8si-{backup}-{ts}); it must be
    persisted on the run status so run_reconcile_timer can find the Job."""
    from k8si.operator.main import on_run_create

    async def _run():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main._patch_run_status") as mock_patch,
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock),
            patch("k8si.operator.main.metrics.record"),
        ):
            mock_k8s = MagicMock()
            mock_k8s.get_namespaced_custom_object.return_value = _BACKUP_OBJ
            mock_k8s_cls.return_value = mock_k8s
            mock_run.return_value = {
                "jobName": "k8si-test-backup-20260818120000",
                "snapshotId": "abc123",
                "sizeBytes": 42,
                "backendType": "restic",
            }

            await on_run_create(
                body={},
                spec=_run_spec(),
                name="test-run",
                namespace="default",
                logger=logging.getLogger("test"),
            )

            success_patch = [
                c.args[2]
                for c in mock_patch.call_args_list
                if c.args[2] and c.args[2].get("phase") == "Succeeded"
            ]
            assert success_patch, "success patch missing"
            assert success_patch[0]["jobName"] == "k8si-test-backup-20260818120000"

    run_coro(_run())


# ── goal #8: restart mid-backup must not duplicate the run ──────────────────


def test_on_run_create_refuses_when_run_already_running_with_live_job():
    """After an operator restart, kopf re-invokes on_run_create for an
    unfinished run while the in-memory _running set is empty. If the run is
    already Running with a live Job, starting a second backup would run two
    Jobs against the same PVC/repo and the orphan sweep could delete the
    in-flight ephemeral PVC — refuse instead and let run_reconcile_timer
    finish the run from the Job state (#8)."""
    from k8si.operator.main import on_run_create

    async def _run():
        with (
            patch("kubernetes.client.CustomObjectsApi") as mock_k8s_cls,
            patch("k8si.operator.main._patch_run_status") as mock_patch,
            patch("k8si.operator.main.workflow.run_backup", new_callable=AsyncMock) as mock_run,
            patch("k8si.operator.main._update_parent_backup", new_callable=AsyncMock),
            patch("k8si.operator.main.metrics.record"),
            patch("k8si.operator.main._run_has_live_job", new_callable=AsyncMock) as mock_live,
        ):
            mock_k8s = MagicMock()
            mock_k8s.get_namespaced_custom_object.return_value = _BACKUP_OBJ
            mock_k8s_cls.return_value = mock_k8s
            mock_live.return_value = True

            await on_run_create(
                body={},
                spec=_run_spec(),
                name="test-run",
                namespace="default",
                logger=logging.getLogger("test"),
            )

            mock_run.assert_not_awaited()
            phases = [
                c.args[2].get("phase")
                for c in mock_patch.call_args_list
                if c.args[2] and c.args[2].get("phase")
            ]
            assert "Running" not in phases

    run_coro(_run())


def test_run_has_live_job_true_when_running_with_existing_job():
    from k8si.operator.main import _run_has_live_job

    with (
        patch("kubernetes.client.CustomObjectsApi") as mock_custom_cls,
        patch("kubernetes.client.BatchV1Api") as mock_batch_cls,
    ):
        custom = MagicMock()
        custom.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Running", "jobName": "k8si-b-20260818"}
        }
        mock_custom_cls.return_value = custom
        mock_batch_cls.return_value.read_namespaced_job.return_value = MagicMock()

        import asyncio

        assert asyncio.run(_run_has_live_job("default", "my-run")) is True


def test_run_has_live_job_false_when_job_gone():
    import kubernetes.client.exceptions

    from k8si.operator.main import _run_has_live_job

    with (
        patch("kubernetes.client.CustomObjectsApi") as mock_custom_cls,
        patch("kubernetes.client.BatchV1Api") as mock_batch_cls,
    ):
        custom = MagicMock()
        custom.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Running", "jobName": "k8si-b-20260818"}
        }
        mock_custom_cls.return_value = custom
        batch = MagicMock()
        exc = kubernetes.client.exceptions.ApiException(status=404)
        batch.read_namespaced_job.side_effect = exc
        mock_batch_cls.return_value = batch

        import asyncio

        assert asyncio.run(_run_has_live_job("default", "my-run")) is False


def test_run_has_live_job_false_for_pending_run():
    from k8si.operator.main import _run_has_live_job

    with patch("kubernetes.client.CustomObjectsApi") as mock_custom_cls:
        custom = MagicMock()
        custom.get_namespaced_custom_object.return_value = {"status": {"phase": "Pending"}}
        mock_custom_cls.return_value = custom

        import asyncio

        assert asyncio.run(_run_has_live_job("default", "my-run")) is False
