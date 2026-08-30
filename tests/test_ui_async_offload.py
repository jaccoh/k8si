"""Event-loop hygiene in the UI's async endpoints.

FastAPI runs `async def` endpoints directly on the event loop — a blocking
kubernetes client call there stalls every concurrent request, including every
open SSE log stream. Sync `def` endpoints are threadpooled and fine. These
tests pin the initial existence checks of the two SSE endpoints to
asyncio.to_thread.
"""

import asyncio
from unittest.mock import patch

from k8si.ui import app as app_module

_REAL_TO_THREAD = asyncio.to_thread


def _spy_to_thread(recorded: list):
    """Async stand-in for asyncio.to_thread that records offloaded function names."""

    async def _spy(func, *args, **kwargs):
        recorded.append(getattr(func, "__name__", str(func)))
        return await _REAL_TO_THREAD(func, *args, **kwargs)

    return _spy


def test_stream_run_logs_offloads_initial_run_lookup() -> None:
    """The pre-stream existence check must go through asyncio.to_thread."""
    recorded: list[str] = []
    spy = _spy_to_thread(recorded)

    with (
        patch("k8si.ui.app.kubernetes.client.CustomObjectsApi") as mock_cls,
        patch("k8si.ui.app.asyncio.to_thread", side_effect=spy),
    ):
        mock_cls.return_value.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Succeeded", "log": []}
        }
        response = asyncio.run(app_module.stream_run_logs("default", "my-run"))

    assert response.status_code == 200
    assert any("get_namespaced_custom_object" in entry for entry in recorded), (
        "initial run lookup must be offloaded via asyncio.to_thread, "
        f"offloaded calls were: {recorded}"
    )


def test_stream_logs_offloads_initial_backup_lookup() -> None:
    """Same for the legacy SSE endpoint's existence check."""
    recorded: list[str] = []
    spy = _spy_to_thread(recorded)

    with (
        patch("k8si.ui.app.kubernetes.client.CustomObjectsApi") as mock_cls,
        patch("k8si.ui.app.asyncio.to_thread", side_effect=spy),
    ):
        mock_cls.return_value.get_namespaced_custom_object.return_value = {
            "status": {"lastBackupResult": "success"}
        }
        response = asyncio.run(app_module.stream_logs("default", "my-backup"))

    assert response.status_code == 200
    assert any("get_namespaced_custom_object" in entry for entry in recorded), (
        "initial backup lookup must be offloaded via asyncio.to_thread, "
        f"offloaded calls were: {recorded}"
    )
