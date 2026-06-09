"""Tests for recentBackups rolling history in k8si/operator/main.py backup_timer."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def _make_patch_dict():
    """Return a dict-like object that mimics kopf.Patch status behaviour.

    kopf.Patch supports attribute access: ``patch.status["key"] = value``
    and ``patch.status.update(d)``.  We use a plain dict with an ``update``
    method, wrapped so ``patch.status`` returns it.
    """

    class _StatusDict(dict):
        def update(self, other=None, **kwargs):  # type: ignore[override]
            if other:
                super().update(other)
            super().update(kwargs)

    class _Patch:
        def __init__(self):
            self.status = _StatusDict()

    return _Patch()


_SUCCESS_RESULT = {
    "lastBackupResult": "success",
    "lastBackupTime": "2026-06-09T02:00:00+00:00",
    "message": "",
}

_SPEC = {
    "schedule": "0 2 * * *",
    "pvc": "test-pvc",
    "resticSecret": "test-secret",
}

_BODY = {"metadata": {"name": "test", "namespace": "default"}}


# ---------------------------------------------------------------------------
# Test 1 — success prepends a "success" entry to recentBackups
# ---------------------------------------------------------------------------


def test_success_prepends_to_recent_backups():
    from k8si.operator import main

    patch_obj = _make_patch_dict()
    logger = logging.getLogger("test")

    # Pre-populate one existing entry so we can verify prepend (not append)
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
                body=_BODY,
                spec=_SPEC,
                name="test",
                namespace="default",
                status={"recentBackups": [existing_entry]},
                patch=patch_obj,
                logger=logger,
            )

    _run(_run_timer())

    recent = patch_obj.status.get("recentBackups", [])
    assert len(recent) == 2, f"Expected 2 entries, got {len(recent)}"
    # Newest entry must be first
    assert recent[0]["result"] == "success"
    assert recent[0]["time"] == _SUCCESS_RESULT["lastBackupTime"]
    # Previous entry is still there
    assert recent[1] == existing_entry


# ---------------------------------------------------------------------------
# Test 2 — failure prepends a "failed" entry to recentBackups
# ---------------------------------------------------------------------------


def test_failure_prepends_to_recent_backups():
    from k8si.operator import main

    patch_obj = _make_patch_dict()
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
                body=_BODY,
                spec=_SPEC,
                name="test",
                namespace="default",
                status={"recentBackups": [existing_entry]},
                patch=patch_obj,
                logger=logger,
            )

    _run(_run_timer())

    recent = patch_obj.status.get("recentBackups", [])
    assert len(recent) == 2, f"Expected 2 entries, got {len(recent)}"
    # Newest entry must be first and marked failed
    assert recent[0]["result"] == "failed"
    assert "time" in recent[0]
    # Previous entry is still there
    assert recent[1] == existing_entry


# ---------------------------------------------------------------------------
# Test 3 — recentBackups is trimmed to 30 entries
# ---------------------------------------------------------------------------


def test_recent_backups_trimmed_to_30():
    from k8si.operator import main

    patch_obj = _make_patch_dict()
    logger = logging.getLogger("test")

    # Start with exactly 30 entries
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
                body=_BODY,
                spec=_SPEC,
                name="test",
                namespace="default",
                status={"recentBackups": list(existing_entries)},
                patch=patch_obj,
                logger=logger,
            )

    _run(_run_timer())

    recent = patch_obj.status.get("recentBackups", [])
    assert len(recent) == 30, f"Expected exactly 30 entries, got {len(recent)}"
    # The newest entry is at the front
    assert recent[0]["time"] == _SUCCESS_RESULT["lastBackupTime"]
    assert recent[0]["result"] == "success"
    # The oldest entry was dropped (last of existing_entries)
    assert recent[-1] != existing_entries[-1]
