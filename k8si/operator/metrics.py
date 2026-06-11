"""Prometheus metrics for the k8si operator."""

from datetime import datetime

from prometheus_client import Gauge, start_http_server

_last_success = Gauge(
    "k8si_backup_last_success_timestamp_seconds",
    "Unix timestamp of the last successful backup (0 if never succeeded)",
    ["name", "namespace"],
)

_last_result = Gauge(
    "k8si_backup_result",
    "Last backup result: 1=success 0=failed -1=running/pending/unknown",
    ["name", "namespace"],
)

_last_duration = Gauge(
    "k8si_backup_duration_seconds",
    "Duration of the last backup run in seconds",
    ["name", "namespace"],
)


def start(port: int = 8000) -> None:
    start_http_server(port)


def record(
    name: str,
    namespace: str,
    result: str,
    last_backup_time: str | None,
    duration: int | None = None,
) -> None:
    if result == "success":
        _last_result.labels(name=name, namespace=namespace).set(1)
        if last_backup_time:
            try:
                ts = datetime.fromisoformat(last_backup_time).timestamp()
                _last_success.labels(name=name, namespace=namespace).set(ts)
            except ValueError:
                pass
    elif result == "failed":
        _last_result.labels(name=name, namespace=namespace).set(0)
    else:
        _last_result.labels(name=name, namespace=namespace).set(-1)
    if duration is not None:
        _last_duration.labels(name=name, namespace=namespace).set(duration)


def remove(name: str, namespace: str) -> None:
    _last_success.remove(name, namespace)
    _last_result.remove(name, namespace)
    _last_duration.remove(name, namespace)
