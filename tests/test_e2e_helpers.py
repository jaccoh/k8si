"""Unit tests for e2e/helpers.py's pure formatting logic."""

from types import SimpleNamespace

from e2e.helpers import _fmt_container_states, _fmt_recent_events


def _state(*, running=False, waiting_reason=None, terminated_reason=None, exit_code=None):
    s = SimpleNamespace(running=None, waiting=None, terminated=None)
    if running:
        s.running = SimpleNamespace()
    if waiting_reason is not None:
        s.waiting = SimpleNamespace(reason=waiting_reason)
    if terminated_reason is not None or exit_code is not None:
        s.terminated = SimpleNamespace(reason=terminated_reason, exit_code=exit_code)
    return s


def _cs(name, *, state=None, last_state=None, restart_count=0):
    return SimpleNamespace(
        name=name,
        state=state or _state(),
        last_state=last_state or _state(),
        restart_count=restart_count,
    )


def _pod(container_statuses):
    return SimpleNamespace(status=SimpleNamespace(container_statuses=container_statuses))


def test_fmt_container_states_running_with_no_restarts():
    pod = _pod([_cs("mariadb", state=_state(running=True), restart_count=0)])
    assert _fmt_container_states(pod) == "mariadb=running(restarts=0)"


def test_fmt_container_states_reports_restart_count():
    pod = _pod([_cs("mariadb", state=_state(running=True), restart_count=3)])
    assert _fmt_container_states(pod) == "mariadb=running(restarts=3)"


def test_fmt_container_states_reports_waiting_reason():
    pod = _pod([_cs("mariadb", state=_state(waiting_reason="CrashLoopBackOff"), restart_count=2)])
    assert _fmt_container_states(pod) == "mariadb=waiting(CrashLoopBackOff, restarts=2)"


def test_fmt_container_states_reports_last_termination():
    pod = _pod(
        [
            _cs(
                "mariadb",
                state=_state(waiting_reason="CrashLoopBackOff"),
                last_state=_state(terminated_reason="OOMKilled", exit_code=137),
                restart_count=1,
            )
        ]
    )
    assert _fmt_container_states(pod) == (
        "mariadb=waiting(CrashLoopBackOff, restarts=1, last=OOMKilled/137)"
    )


def test_fmt_container_states_no_statuses_returns_empty_string():
    assert _fmt_container_states(_pod(None)) == ""


def _event(*, reason, message, count=1):
    return SimpleNamespace(reason=reason, message=message, count=count)


def test_fmt_recent_events_formats_reason_and_message():
    events = [_event(reason="Unhealthy", message="Readiness probe failed: timeout", count=3)]
    assert _fmt_recent_events(events) == "Unhealthy(x3): Readiness probe failed: timeout"


def test_fmt_recent_events_joins_multiple_with_semicolon():
    events = [
        _event(reason="Unhealthy", message="Readiness probe failed: timeout", count=3),
        _event(reason="BackOff", message="Back-off restarting failed container", count=1),
    ]
    assert _fmt_recent_events(events) == (
        "Unhealthy(x3): Readiness probe failed: timeout; "
        "BackOff(x1): Back-off restarting failed container"
    )


def test_fmt_recent_events_empty_list_returns_empty_string():
    assert _fmt_recent_events([]) == ""
