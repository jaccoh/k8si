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
            assert "Running" in phases
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
