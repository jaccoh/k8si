"""Env parsing and sizing for the operator's pacing knobs (pool.py).

Module-level values are read at import time, so tests that exercise an
override reload the module; the autouse fixture reloads it once more with a
clean environment afterwards so knob values never leak into other tests.
"""

import importlib
import os

import pytest

import k8si.operator.pool as pool

_KNOBS = ("K8SI_MAX_CONCURRENT_BACKUPS", "K8SI_BACKUP_SETTLE_SECONDS")


@pytest.fixture(autouse=True)
def _restore_pool():
    yield
    pool.EXECUTOR.shutdown(wait=False)
    for knob in _KNOBS:
        os.environ.pop(knob, None)
    importlib.reload(pool)


def _reload_with_env(**env: str):
    pool.EXECUTOR.shutdown(wait=False)
    for knob in _KNOBS:
        os.environ.pop(knob, None)
    os.environ.update(env)
    return importlib.reload(pool)


# ── _env_int parsing ──────────────────────────────────────────────────────────


def test_env_int_defaults_when_absent():
    assert pool._env_int("K8SI_NO_SUCH_KNOB", 7) == 7


def test_env_int_parses_and_strips():
    os.environ["K8SI_NO_SUCH_KNOB"] = " 5 "
    assert pool._env_int("K8SI_NO_SUCH_KNOB", 7) == 5


def test_env_int_garbage_falls_back_to_default():
    os.environ["K8SI_NO_SUCH_KNOB"] = "soon"
    assert pool._env_int("K8SI_NO_SUCH_KNOB", 7) == 7


def test_env_int_clamps_to_minimum():
    os.environ["K8SI_NO_SUCH_KNOB"] = "-3"
    assert pool._env_int("K8SI_NO_SUCH_KNOB", 7, minimum=0) == 0


# ── knob defaults and overrides ───────────────────────────────────────────────


def test_defaults_without_env():
    reloaded = _reload_with_env()
    assert reloaded.MAX_CONCURRENT_BACKUPS == 2
    assert reloaded.BACKUP_SETTLE_SECONDS == 0
    assert reloaded.SEMAPHORE._value == 2


def test_concurrency_knob_resizes_semaphore_and_executor():
    reloaded = _reload_with_env(K8SI_MAX_CONCURRENT_BACKUPS="5")
    assert reloaded.MAX_CONCURRENT_BACKUPS == 5
    assert reloaded.SEMAPHORE._value == 5
    # Every running backup parks one executor worker for the whole job
    # duration — the pool must have room for all of them.
    assert reloaded.EXECUTOR._max_workers >= reloaded.MAX_CONCURRENT_BACKUPS


def test_concurrency_knob_refuses_to_go_below_one():
    reloaded = _reload_with_env(K8SI_MAX_CONCURRENT_BACKUPS="0")
    assert reloaded.MAX_CONCURRENT_BACKUPS == 1


def test_settle_knob_reads_env():
    reloaded = _reload_with_env(K8SI_BACKUP_SETTLE_SECONDS="90")
    assert reloaded.BACKUP_SETTLE_SECONDS == 90


def test_garbage_knob_values_fall_back_to_defaults():
    reloaded = _reload_with_env(
        K8SI_MAX_CONCURRENT_BACKUPS="many",
        K8SI_BACKUP_SETTLE_SECONDS="a bit",
    )
    assert reloaded.MAX_CONCURRENT_BACKUPS == 2
    assert reloaded.BACKUP_SETTLE_SECONDS == 0


# ── settle gap bookkeeping ────────────────────────────────────────────────────


def test_settle_gap_noop_when_never_finished(monkeypatch):
    """Fresh operator: no recorded finish means nothing to settle out."""
    import asyncio
    import time

    monkeypatch.setattr(pool, "BACKUP_SETTLE_SECONDS", 300)
    monkeypatch.setattr(pool, "_last_finish_monotonic", 0.0)
    started = time.monotonic()
    asyncio.run(pool.settle_gap())
    assert time.monotonic() - started < 0.05


def test_settle_gap_sleeps_remaining_time(monkeypatch):
    import asyncio
    import time

    monkeypatch.setattr(pool, "BACKUP_SETTLE_SECONDS", 0.2)
    pool.note_backup_finished()
    started = time.monotonic()
    asyncio.run(pool.settle_gap())
    assert time.monotonic() - started >= 0.15


def test_settle_gap_noop_when_time_already_elapsed(monkeypatch):
    import asyncio
    import time

    monkeypatch.setattr(pool, "BACKUP_SETTLE_SECONDS", 0.01)
    pool.note_backup_finished()
    time.sleep(0.05)  # gap fully elapsed
    started = time.monotonic()
    asyncio.run(pool.settle_gap())
    assert time.monotonic() - started < 0.05
