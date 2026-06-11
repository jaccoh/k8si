"""Tests for duration gauge in k8si/operator/metrics.py."""


def test_record_sets_duration_on_success():
    """record() with duration sets the k8si_backup_duration_seconds gauge."""
    from k8si.operator.metrics import _last_duration, record

    record("dur-test", "ns-test", "success", None, duration=45)
    val = _last_duration.labels(name="dur-test", namespace="ns-test")._value.get()
    assert val == 45.0


def test_record_sets_duration_on_failure():
    """record() with duration also sets the gauge on failure."""
    from k8si.operator.metrics import _last_duration, record

    record("dur-fail", "ns-test", "failed", None, duration=12)
    val = _last_duration.labels(name="dur-fail", namespace="ns-test")._value.get()
    assert val == 12.0


def test_record_skips_duration_when_none():
    """record() without duration does not raise."""
    from k8si.operator.metrics import record

    record("dur-none", "ns-test", "success", None)


def test_record_sets_last_success_timestamp():
    """record() with a valid ISO timestamp sets _last_success gauge."""
    from k8si.operator.metrics import _last_success, record

    record("ts-test", "ns-test", "success", "2026-06-12T02:00:00+00:00")
    val = _last_success.labels(name="ts-test", namespace="ns-test")._value.get()
    assert val > 0


def test_record_running_result_sets_minus_one():
    """record() with result='running' sets _last_result gauge to -1."""
    from k8si.operator.metrics import _last_result, record

    record("run-test", "ns-test", "running", None)
    val = _last_result.labels(name="run-test", namespace="ns-test")._value.get()
    assert val == -1.0


def test_remove_clears_labels():
    """remove() does not raise when labels exist."""
    from k8si.operator.metrics import record, remove

    record("rm-test", "ns-test", "success", "2026-06-12T02:00:00+00:00", duration=10)
    remove("rm-test", "ns-test")


def test_record_invalid_timestamp_does_not_raise():
    """record() with a malformed timestamp swallows the ValueError without raising."""
    from k8si.operator.metrics import record

    record("bad-ts", "ns-test", "success", "not-a-date")


def test_start_calls_prometheus_http_server():
    """start() calls start_http_server with the given port."""
    from unittest.mock import patch

    with patch("k8si.operator.metrics.start_http_server") as mock_srv:
        from k8si.operator.metrics import start

        start(port=9999)

    mock_srv.assert_called_once_with(9999)
