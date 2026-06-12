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


# ---------------------------------------------------------------------------
# _expand_db_host: short hostname → expanded to FQDN
# ---------------------------------------------------------------------------


def test_expand_db_host_short_name_expands():
    from k8si.operator.quiesce import _expand_db_host

    creds = {"DB_HOST": "mariadb", "DB_USER": "root"}
    result = _expand_db_host(creds, "mynamespace")
    assert result["DB_HOST"] == "mariadb.mynamespace.svc.cluster.local"


def test_expand_db_host_fqdn_unchanged():
    from k8si.operator.quiesce import _expand_db_host

    creds = {"DB_HOST": "mariadb.default.svc.cluster.local", "DB_USER": "root"}
    result = _expand_db_host(creds, "default")
    assert result["DB_HOST"] == "mariadb.default.svc.cluster.local"


def test_expand_db_host_no_host_key_unchanged():
    from k8si.operator.quiesce import _expand_db_host

    creds = {"DB_USER": "root"}
    result = _expand_db_host(creds, "default")
    assert "DB_HOST" not in result


# ---------------------------------------------------------------------------
# _read_secret_sync: decodes base64-encoded secret data
# ---------------------------------------------------------------------------


def test_read_secret_sync_decodes_base64():
    import base64

    from k8si.operator.quiesce import _read_secret_sync

    mock_secret = MagicMock()
    mock_secret.data = {
        "DB_PASSWORD": base64.b64encode(b"s3cr3t").decode(),
        "DB_USER": base64.b64encode(b"root").decode(),
    }
    mock_v1 = MagicMock()
    mock_v1.read_namespaced_secret.return_value = mock_secret

    with patch("k8si.operator.quiesce.kubernetes.client.CoreV1Api", return_value=mock_v1):
        result = _read_secret_sync("myns", "my-secret")

    assert result["DB_PASSWORD"] == "s3cr3t"
    assert result["DB_USER"] == "root"


# ---------------------------------------------------------------------------
# _find_pod_name_sync: finds running pods
# ---------------------------------------------------------------------------


def test_find_pod_name_sync_returns_running_pod():
    from k8si.operator.quiesce import _find_pod_name_sync

    pod = MagicMock()
    pod.status.phase = "Running"
    pod.metadata.name = "app-abc123"
    mock_v1 = MagicMock()
    mock_v1.list_namespaced_pod.return_value.items = [pod]

    with patch("k8si.operator.quiesce.kubernetes.client.CoreV1Api", return_value=mock_v1):
        result = _find_pod_name_sync("myns", {"app": "myapp"})

    assert result == "app-abc123"


def test_find_pod_name_sync_no_running_pods_raises():
    from k8si.operator.quiesce import _find_pod_name_sync

    pod = MagicMock()
    pod.status.phase = "Pending"
    mock_v1 = MagicMock()
    mock_v1.list_namespaced_pod.return_value.items = [pod]

    with patch("k8si.operator.quiesce.kubernetes.client.CoreV1Api", return_value=mock_v1):
        with pytest.raises(RuntimeError, match="No running pod"):
            _find_pod_name_sync("myns", {"app": "myapp"})


# ---------------------------------------------------------------------------
# _mariadb_ftwrl_sync: issues FLUSH TABLES WITH READ LOCK
# ---------------------------------------------------------------------------


def test_mariadb_ftwrl_sync_executes_lock():
    from k8si.operator.quiesce import _mariadb_ftwrl_sync

    mock_conn = MagicMock()
    mock_pymysql = MagicMock()
    mock_pymysql.connect.return_value = mock_conn

    creds = {"DB_HOST": "db", "DB_PORT": "3306", "DB_USER": "root", "DB_PASSWORD": "pw"}
    with patch.dict("sys.modules", {"pymysql": mock_pymysql}):
        result = _mariadb_ftwrl_sync(creds)

    mock_conn.cursor.return_value.__enter__.return_value.execute.assert_called_once_with(
        "FLUSH TABLES WITH READ LOCK"
    )
    assert result is mock_conn


# ---------------------------------------------------------------------------
# _mariadb_unlock_sync: issues UNLOCK TABLES and closes connection
# ---------------------------------------------------------------------------


def test_mariadb_unlock_sync_executes_unlock_and_closes():
    from k8si.operator.quiesce import _mariadb_unlock_sync

    mock_conn = MagicMock()
    _mariadb_unlock_sync(mock_conn)

    mock_conn.cursor.return_value.__enter__.return_value.execute.assert_called_once_with(
        "UNLOCK TABLES"
    )
    mock_conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# _postgres_checkpoint_sync: issues CHECKPOINT
# ---------------------------------------------------------------------------


def test_postgres_checkpoint_sync_executes_checkpoint():
    from k8si.operator.quiesce import _postgres_checkpoint_sync

    mock_conn = MagicMock()
    mock_psycopg = MagicMock()
    mock_psycopg.connect.return_value = mock_conn

    creds = {"DB_HOST": "pg", "DB_PORT": "5432", "DB_USER": "postgres", "DB_PASSWORD": "pw"}
    with patch.dict("sys.modules", {"psycopg": mock_psycopg}):
        _postgres_checkpoint_sync(creds)

    mock_conn.cursor.return_value.__enter__.return_value.execute.assert_called_once_with(
        "CHECKPOINT"
    )
    mock_conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# _sqlite_checkpoint_sync: streams command into pod
# ---------------------------------------------------------------------------


def test_sqlite_checkpoint_sync_calls_stream():
    from k8si.operator.quiesce import _sqlite_checkpoint_sync

    mock_v1 = MagicMock()
    with (
        patch("k8si.operator.quiesce.kubernetes.client.CoreV1Api", return_value=mock_v1),
        patch("kubernetes.stream.stream", return_value="checkpointed /data/db.sqlite3"),
    ):
        _sqlite_checkpoint_sync("myns", "app-pod", ["/data/db.sqlite3"])


# ---------------------------------------------------------------------------
# quiesce_context: postgres, sqlite, and unknown-type branches
# ---------------------------------------------------------------------------


def test_quiesce_context_postgres_yields():
    async def _inner():
        entered = False
        with (
            patch("k8si.operator.quiesce._read_secret_sync", return_value=_FAKE_CREDS),
            patch("k8si.operator.quiesce._postgres_checkpoint_sync"),
        ):
            async with quiesce_context(
                {"type": "postgres", "secretRef": "pg-secret"}, "default", logging.getLogger("test")
            ):
                entered = True
        return entered

    assert asyncio.run(_inner())


def test_quiesce_context_sqlite_with_selector_yields():
    async def _inner():
        entered = False
        with (
            patch("k8si.operator.quiesce._find_pod_name_sync", return_value="app-pod"),
            patch("k8si.operator.quiesce._sqlite_checkpoint_sync"),
        ):
            async with quiesce_context(
                {
                    "type": "sqlite",
                    "podSelector": {"app": "myapp"},
                    "dbPaths": ["/data/db.sqlite3"],
                },
                "default",
                logging.getLogger("test"),
            ):
                entered = True
        return entered

    assert asyncio.run(_inner())


def test_quiesce_context_sqlite_without_selector_logs_warning():
    async def _inner():
        entered = False
        async with quiesce_context({"type": "sqlite"}, "default", logging.getLogger("test")):
            entered = True
        return entered

    assert asyncio.run(_inner())


def test_quiesce_context_unknown_type_raises():
    async def _inner():
        with pytest.raises(ValueError, match="Unknown database.type"):
            async with quiesce_context({"type": "redis"}, "default", logging.getLogger("test")):
                pass

    asyncio.run(_inner())
