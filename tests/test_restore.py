"""Tests for restore (init container) mode."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from k8si.backend import BackupError
from k8si.config import Config
from k8si.restore import MARKER_FILE, NO_RESTORE_FILE, run


def make_config(
    tmp_path: Path,
    sentinels: list[str] | None = None,
    required: bool = False,
    restore_tags: list[str] | None = None,
    max_age_hours: float | None = None,
    size_min: int | None = None,
    size_max: int | None = None,
    restore_snapshot: str | None = None,
) -> Config:
    return Config(
        mode="restore",
        data_path=tmp_path,
        restic_repository="sftp:fake",
        restic_password="secret",
        restic_password_file=None,
        restore_sentinels=sentinels if sentinels is not None else ["config.xml"],
        restore_required=required,
        restore_tags=restore_tags or [],
        restore_max_age_hours=max_age_hours,
        restore_size_min=size_min,
        restore_size_max=size_max,
        restore_snapshot=restore_snapshot,
    )


def _backend_with_snapshot(
    snapshot_id: str = "abc12345", paths: list[str] | None = None
) -> MagicMock:
    backend = MagicMock()
    backend.snapshots.return_value = [{
        "id": snapshot_id,
        "short_id": snapshot_id[:8],
        "time": "2026-05-07T19:00:00Z",
        "tags": [],
    }]
    backend.ls.return_value = paths if paths is not None else ["/data/config.xml"]
    backend.snapshot_size.return_value = 10 * 1024 * 1024  # 10 MiB
    return backend


# ── skip conditions ────────────────────────────────────────────────────────────

def test_skips_when_all_sentinels_present(tmp_path: Path) -> None:
    (tmp_path / "config.xml").touch()
    backend = MagicMock()
    run(make_config(tmp_path), backend)
    backend.restore.assert_not_called()


def test_skips_when_no_restore_file_present(tmp_path: Path) -> None:
    (tmp_path / NO_RESTORE_FILE).touch()
    backend = MagicMock()
    run(make_config(tmp_path), backend)
    backend.restore.assert_not_called()


def test_skips_when_marker_present_no_sentinels(tmp_path: Path) -> None:
    (tmp_path / MARKER_FILE).write_text("restored\n")
    backend = MagicMock()
    run(make_config(tmp_path, sentinels=[]), backend)
    backend.restore.assert_not_called()


def test_skips_when_no_snapshots_not_required(tmp_path: Path) -> None:
    backend = MagicMock()
    backend.snapshots.return_value = []
    run(make_config(tmp_path), backend)
    backend.restore.assert_not_called()


# ── fail-loud conditions ───────────────────────────────────────────────────────

def test_fails_when_marker_present_but_sentinels_missing(tmp_path: Path) -> None:
    (tmp_path / MARKER_FILE).write_text("restored\n")
    backend = MagicMock()
    with pytest.raises(SystemExit):
        run(make_config(tmp_path), backend)


def test_fails_when_no_snapshots_and_required(tmp_path: Path) -> None:
    backend = MagicMock()
    backend.snapshots.return_value = []
    with pytest.raises(SystemExit):
        run(make_config(tmp_path, required=True), backend)


def test_fails_when_backend_restore_errors(tmp_path: Path) -> None:
    backend = _backend_with_snapshot()
    backend.restore.side_effect = BackupError("failed", 1, "connection refused")
    with pytest.raises(SystemExit):
        run(make_config(tmp_path), backend)


# ── snapshot quality gates ─────────────────────────────────────────────────────

def test_skips_when_sentinel_missing_from_snapshot(tmp_path: Path) -> None:
    backend = _backend_with_snapshot(paths=["/data/other-file.txt"])
    run(make_config(tmp_path), backend)
    backend.restore.assert_not_called()


def test_skips_when_snapshot_too_small(tmp_path: Path) -> None:
    backend = _backend_with_snapshot()
    backend.snapshot_size.return_value = 100  # 100 bytes
    run(make_config(tmp_path, size_min=1024 * 1024), backend)
    backend.restore.assert_not_called()


def test_skips_when_snapshot_too_large(tmp_path: Path) -> None:
    backend = _backend_with_snapshot()
    backend.snapshot_size.return_value = 100 * 1024 * 1024  # 100 MiB
    run(make_config(tmp_path, size_max=50 * 1024 * 1024), backend)
    backend.restore.assert_not_called()


# ── successful restore ─────────────────────────────────────────────────────────

def test_restores_and_writes_marker(tmp_path: Path) -> None:
    backend = _backend_with_snapshot()
    backend.restore.side_effect = lambda **_: (tmp_path / "config.xml").touch()
    run(make_config(tmp_path), backend)
    backend.restore.assert_called_once()
    assert (tmp_path / MARKER_FILE).exists()


def test_restore_uses_pinned_snapshot(tmp_path: Path) -> None:
    backend = _backend_with_snapshot()
    backend.restore.side_effect = lambda **_: (tmp_path / "config.xml").touch()
    run(make_config(tmp_path, restore_snapshot="deadbeef"), backend)
    backend.restore.assert_called_once_with(snapshot_id="deadbeef")
    backend.snapshots.assert_not_called()


def test_restore_passes_tags_to_snapshots(tmp_path: Path) -> None:
    backend = _backend_with_snapshot()
    backend.restore.side_effect = lambda **_: (tmp_path / "config.xml").touch()
    run(make_config(tmp_path, restore_tags=["app=prowlarr"]), backend)
    backend.snapshots.assert_called_once_with(tags=["app=prowlarr"])


def test_fails_when_sentinels_missing_after_restore(tmp_path: Path) -> None:
    backend = _backend_with_snapshot()
    # restore completes but sentinel is never written
    with pytest.raises(SystemExit):
        run(make_config(tmp_path), backend)
