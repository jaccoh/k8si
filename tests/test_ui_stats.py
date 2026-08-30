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


# ── stats must describe the same history the sparkline renders ────────────────


def _item(recent_backups: list, recent_runs: list | None = None) -> dict:
    status: dict = {"recentBackups": recent_backups}
    if recent_runs is not None:
        status["recentRuns"] = recent_runs
    return {
        "metadata": {"name": "b", "namespace": "default"},
        "spec": {},
        "status": status,
    }


def test_shape_stats_prefer_recent_runs_when_present() -> None:
    """The dashboard sparkline renders recentRuns; the % and streak must be
    computed from the same list — otherwise the number describes a different
    history than the bars next to it."""
    from k8si.ui.app import _shape

    shaped = _shape(
        _item(
            recent_backups=[{"result": "success"}] * 4,  # legacy history: all green
            recent_runs=[{"result": "failed"}, {"result": "failed"}],  # live truth
        )
    )

    assert shaped["successRate"] == 0.0
    assert shaped["streak"] == -2


def test_shape_stats_fall_back_to_recent_backups_without_runs() -> None:
    """Pre-0.9 backups only carry recentBackups — stats still work for them."""
    from k8si.ui.app import _shape

    shaped = _shape(
        _item(
            recent_backups=[{"result": "success"}, {"result": "failed"}],
            recent_runs=None,  # absent
        )
    )

    assert shaped["successRate"] == 0.5
    assert shaped["streak"] == 1


def test_shape_stats_fall_back_when_recent_runs_empty() -> None:
    """An empty recentRuns list carries no signal — fall back, don't zero out."""
    from k8si.ui.app import _shape

    shaped = _shape(
        _item(
            recent_backups=[{"result": "success"}],
            recent_runs=[],
        )
    )

    assert shaped["successRate"] == 1.0
    assert shaped["streak"] == 1
