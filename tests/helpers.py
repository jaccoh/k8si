"""Shared test utilities for k8si tests.

Import from test files:
    from tests.helpers import FakePatch, SPEC, BODY, run_coro, popen_ctx, make_ui_client
"""

import asyncio
from unittest.mock import MagicMock

SPEC = {"schedule": "0 2 * * *", "pvc": "test-pvc", "resticSecret": "test-secret"}
BODY = {"metadata": {"name": "test", "namespace": "default"}}


class _StatusDict(dict):
    """Dict subclass that mimics kopf.Patch.status update behaviour."""

    def update(self, other=None, **kwargs):  # type: ignore[override]
        if other is not None:
            super().update(other)
        super().update(kwargs)


class FakePatch:
    """Minimal kopf.Patch stand-in for operator tests."""

    def __init__(self) -> None:
        self.status = _StatusDict()


def run_coro(coro):  # type: ignore[no-untyped-def]
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def popen_ctx(lines: list[str], returncode: int = 0) -> MagicMock:
    """Return a context-manager mock that yields *lines* from stdout.

    Use with ``patch("subprocess.Popen", return_value=popen_ctx(...))``.
    """
    proc = MagicMock()
    proc.stdout = iter(lines)
    proc.returncode = returncode
    proc.communicate.return_value = ("", "")
    ctx = MagicMock()
    ctx.__enter__.return_value = proc
    ctx.__exit__.return_value = False
    return ctx


def make_ui_client(raise_server_exceptions: bool = True):  # type: ignore[no-untyped-def]
    """Return a FastAPI TestClient with K8s startup disabled."""
    from fastapi.testclient import TestClient

    from k8si.ui import app as app_module

    app_module.app.router.on_startup.clear()
    from k8si.ui.app import app

    return TestClient(app, raise_server_exceptions=raise_server_exceptions)
