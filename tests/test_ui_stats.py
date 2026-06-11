"""Tests for _compute_stats() helper in k8si/ui/app.py."""

import pytest


@pytest.mark.parametrize(
    "recent, expected",
    [
        (
            [],
            {"successRate": None, "streak": 0},
        ),
        (
            [{"result": "success"}],
            {"successRate": 1.0, "streak": 1},
        ),
        (
            [{"result": "failed"}],
            {"successRate": 0.0, "streak": -1},
        ),
        (
            [
                {"result": "success"},
                {"result": "success"},
                {"result": "failed"},
            ],
            {"successRate": pytest.approx(0.667, abs=0.001), "streak": 2},
        ),
        (
            [
                {"result": "failed"},
                {"result": "success"},
                {"result": "success"},
            ],
            {"successRate": pytest.approx(0.667, abs=0.001), "streak": -1},
        ),
        (
            [
                {"result": "failed"},
                {"result": "failed"},
                {"result": "success"},
            ],
            {"successRate": pytest.approx(0.333, abs=0.001), "streak": -2},
        ),
        (
            [
                {"result": "success"},
                {"result": "success"},
                {"result": "success"},
            ],
            {"successRate": 1.0, "streak": 3},
        ),
        (
            [{"result": "running"}],
            {"successRate": 0.0, "streak": 0},
        ),
    ],
)
def test_compute_stats(recent: list, expected: dict) -> None:
    from k8si.ui.app import _compute_stats

    result = _compute_stats(recent)
    assert result["successRate"] == expected["successRate"]
    assert result["streak"] == expected["streak"]
