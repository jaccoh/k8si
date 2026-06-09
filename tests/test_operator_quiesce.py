"""Tests for k8si/operator/quiesce.py."""

import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest

from k8si.operator.quiesce import quiesce_context

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MARIADB_SPEC = {
    "type": "mariadb",
    "secretRef": "mariadb-secret",
}

_FAKE_CREDS = {
    "DB_HOST": "mariadb.default.svc.cluster.local",
    "DB_PORT": "3306",
    "DB_USER": "root",
    "DB_PASSWORD": "secret",
    "DB_NAME": "mydb",
}


def _run(coro):
    """Run an async coroutine synchronously (no extra deps required)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Test 1 — no-op when db_spec is None
# ---------------------------------------------------------------------------


def test_no_op_when_db_spec_is_none():
    logger = logging.getLogger("test")

    async def _inner():
        entered = False
        async with quiesce_context(None, "default", logger):
            entered = True
        return entered

    assert _run(_inner())


# ---------------------------------------------------------------------------
# Test 2 — MariaDB quiesce emits a WARNING about lock hold time before yielding
# ---------------------------------------------------------------------------


def test_mariadb_quiesce_logs_warning_before_yield():
    import k8si.operator.quiesce as qmod

    logger = logging.getLogger("test_warning")
    warning_records_before_yield: list[logging.LogRecord] = []

    async def _inner():
        handler: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record):
                handler.append(record)

        capture = _Capture()
        capture.setLevel(logging.WARNING)
        qmod.log.addHandler(capture)
        try:
            with (
                patch(
                    "k8si.operator.quiesce._read_secret_sync",
                    return_value=_FAKE_CREDS,
                ),
                patch(
                    "k8si.operator.quiesce._mariadb_ftwrl_sync",
                    return_value=MagicMock(),
                ),
                patch("k8si.operator.quiesce._mariadb_unlock_sync"),
            ):
                async with quiesce_context(_MARIADB_SPEC, "default", logger):
                    # Collect records emitted *before* yield (i.e. during context setup)
                    warning_records_before_yield.extend(
                        r for r in handler if r.levelno == logging.WARNING
                    )
        finally:
            qmod.log.removeHandler(capture)

    _run(_inner())

    assert warning_records_before_yield, "Expected at least one WARNING before yield"
    messages = [r.getMessage() for r in warning_records_before_yield]
    assert any(
        "lock" in m.lower() or "FTWRL" in m or "write lock" in m.lower() for m in messages
    ), f"WARNING should mention the lock, got: {messages}"


# ---------------------------------------------------------------------------
# Test 3 — MariaDB quiesce logs elapsed time on release
# ---------------------------------------------------------------------------


def test_mariadb_quiesce_logs_elapsed_on_release():
    import k8si.operator.quiesce as qmod

    info_records_after_yield: list[logging.LogRecord] = []

    async def _inner():
        handler: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record):
                handler.append(record)

        capture = _Capture()
        capture.setLevel(logging.DEBUG)
        original_level = qmod.log.level
        qmod.log.setLevel(logging.DEBUG)
        qmod.log.addHandler(capture)
        try:
            with (
                patch(
                    "k8si.operator.quiesce._read_secret_sync",
                    return_value=_FAKE_CREDS,
                ),
                patch(
                    "k8si.operator.quiesce._mariadb_ftwrl_sync",
                    return_value=MagicMock(),
                ),
                patch("k8si.operator.quiesce._mariadb_unlock_sync"),
            ):
                async with quiesce_context(
                    _MARIADB_SPEC, "default", logging.getLogger("test_elapsed")
                ):
                    # Records captured up to this point belong to setup
                    records_during_setup = list(handler)

            # After context exits the finally block has run
            records_after_exit = [r for r in handler if r not in records_during_setup]
            info_records_after_yield.extend(
                r for r in records_after_exit if r.levelno == logging.INFO
            )
        finally:
            qmod.log.removeHandler(capture)
            qmod.log.setLevel(original_level)

    _run(_inner())

    assert info_records_after_yield, "Expected at least one INFO after context exit"
    messages = [r.getMessage() for r in info_records_after_yield]
    # The message should mention elapsed time (e.g. "releasing write lock after 0.0s")
    assert any(
        "s" in m and ("elapsed" in m.lower() or "releasing" in m.lower() or "after" in m.lower())
        for m in messages
    ), f"INFO on release should mention elapsed time, got: {messages}"


# ---------------------------------------------------------------------------
# Test 4 — unlock is called even when the body raises
# ---------------------------------------------------------------------------


def test_mariadb_unlock_called_on_exception():
    unlock_calls: list = []

    def fake_unlock(conn):
        unlock_calls.append(conn)

    async def _inner():
        with (
            patch(
                "k8si.operator.quiesce._read_secret_sync",
                return_value=_FAKE_CREDS,
            ),
            patch(
                "k8si.operator.quiesce._mariadb_ftwrl_sync",
                return_value=MagicMock(),
            ),
            patch("k8si.operator.quiesce._mariadb_unlock_sync", side_effect=fake_unlock),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                async with quiesce_context(_MARIADB_SPEC, "default", logging.getLogger("test_exc")):
                    raise RuntimeError("boom")

    _run(_inner())

    assert len(unlock_calls) == 1, f"unlock must be called exactly once, got {len(unlock_calls)}"
