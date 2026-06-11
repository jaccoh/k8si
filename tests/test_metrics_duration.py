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
