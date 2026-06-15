"""Tests for GET /api/runs/{namespace}/{run_name}/logs SSE endpoint in k8si/ui/app.py."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import kubernetes.client.exceptions
from fastapi.testclient import TestClient

from tests.helpers import make_ui_client


def _make_client() -> TestClient:
    return make_ui_client()


def _run_obj(phase: str = "Succeeded", log: list | None = None) -> dict:
    return {
        "metadata": {"name": "mybackup-20260614120000"},
        "status": {
            "phase": phase,
            "log": log or [],
            "startTime": "2026-06-14T12:00:00+00:00",
            "completionTime": "2026-06-14T12:04:27+00:00",
            "message": "",
        },
    }


def _sse_events(resp) -> list[dict]:
    """Parse SSE response body into list of data events."""
    events = []
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


# ── fast initial 404 ──────────────────────────────────────────────────────────


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_stream_run_logs_404_on_missing_run(mock_api_cls: MagicMock) -> None:
    """GET /api/runs/.../logs returns 404 immediately when the run doesn't exist."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.side_effect = kubernetes.client.exceptions.ApiException(
        status=404
    )

    client = _make_client()
    resp = client.get("/api/runs/default/nonexistent-run/logs")

    assert resp.status_code == 404


@patch("k8si.ui.app.asyncio.sleep", new_callable=AsyncMock)
@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_stream_run_logs_emits_error_event_on_404_during_stream(
    mock_api_cls: MagicMock,
    _mock_sleep: AsyncMock,
) -> None:
    """SSE generator emits error event and stops if run disappears mid-stream (404)."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api

    # Initial existence check passes; subsequent poll returns 404
    poll_exc = kubernetes.client.exceptions.ApiException(status=404)
    mock_api.get_namespaced_custom_object.side_effect = [
        _run_obj("Running"),  # initial existence check
        poll_exc,  # first poll inside _generate
    ]

    client = _make_client()
    resp = client.get("/api/runs/default/mybackup-20260614120000/logs")

    events = _sse_events(resp)
    assert any(e.get("type") == "error" for e in events), f"no error event in {events}"
    error_event = next(e for e in events if e.get("type") == "error")
    assert "not found" in error_event.get("message", "").lower()


# ── consecutive error surfacing ───────────────────────────────────────────────


@patch("k8si.ui.app.asyncio.sleep", new_callable=AsyncMock)
@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_stream_run_logs_surfaces_error_after_5_consecutive_failures(
    mock_api_cls: MagicMock,
    _mock_sleep: AsyncMock,
) -> None:
    """SSE generator emits error event and stops after 5 consecutive non-404 API errors."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api

    api_err = kubernetes.client.exceptions.ApiException(status=503)
    mock_api.get_namespaced_custom_object.side_effect = [
        _run_obj("Running"),  # initial existence check
        api_err,
        api_err,
        api_err,
        api_err,
        api_err,  # 5th consecutive failure → error event
    ]

    client = _make_client()
    resp = client.get("/api/runs/default/mybackup-20260614120000/logs")

    events = _sse_events(resp)
    assert any(e.get("type") == "error" for e in events), f"no error event in {events}"
    error_event = next(e for e in events if e.get("type") == "error")
    assert "5" in error_event.get("message", "")


# ── normal completion path ────────────────────────────────────────────────────


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_stream_run_logs_emits_done_on_succeeded(mock_api_cls: MagicMock) -> None:
    """SSE generator emits done event with result=success when phase=Succeeded."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.return_value = _run_obj("Succeeded")

    client = _make_client()
    resp = client.get("/api/runs/default/mybackup-20260614120000/logs")

    events = _sse_events(resp)
    done = next((e for e in events if e.get("type") == "done"), None)
    assert done is not None
    assert done["result"] == "success"
    assert done["phase"] == "Succeeded"
    assert "startTime" in done
    assert "completionTime" in done


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_stream_run_logs_emits_done_on_failed(mock_api_cls: MagicMock) -> None:
    """SSE generator emits done event with result=failed when phase=Failed."""
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.return_value = _run_obj("Failed")

    client = _make_client()
    resp = client.get("/api/runs/default/mybackup-20260614120000/logs")

    events = _sse_events(resp)
    assert any(e.get("type") == "done" and e.get("result") == "failed" for e in events)


@patch("k8si.ui.app.kubernetes.client.CustomObjectsApi")
def test_stream_run_logs_streams_log_entries(mock_api_cls: MagicMock) -> None:
    """SSE generator emits phase events for each log entry before the done event."""
    log = [
        {"time": "2026-06-14T12:00:01Z", "phase": "snapshot", "message": "creating snapshot"},
        {"time": "2026-06-14T12:00:05Z", "phase": "upload", "message": "uploading data"},
    ]
    mock_api = MagicMock()
    mock_api_cls.return_value = mock_api
    mock_api.get_namespaced_custom_object.return_value = _run_obj("Succeeded", log=log)

    client = _make_client()
    resp = client.get("/api/runs/default/mybackup-20260614120000/logs")

    events = _sse_events(resp)
    phase_events = [e for e in events if e.get("type") == "phase"]
    assert len(phase_events) == 2
    assert phase_events[0]["message"] == "creating snapshot"
    assert phase_events[1]["message"] == "uploading data"
