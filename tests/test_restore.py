"""Tests for restore (init container) mode."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from k8si.config import Config
from k8si.restic import ResticError, ResticNoSnapshotsError
from k8si.restore import run


def make_config(tmp_path: Path, sentinel: str = ".initialized") -> Config:
    return Config(
        mode="restore",
        data_path=tmp_path,
        restic_repository="sftp:fake",
        restic_password="secret",
        restic_password_file=None,
        sentinel_file=sentinel,
        backup_schedule=None,
        retention_daily=7,
        retention_weekly=4,
        retention_monthly=3,
        pre_backup_hook=None,
    )


def test_skips_restore_when_sentinel_present(tmp_path: Path) -> None:
    (tmp_path / ".initialized").touch()
    restic = MagicMock()
    run(make_config(tmp_path), restic)
    restic.restore.assert_not_called()


def test_restores_when_sentinel_missing(tmp_path: Path) -> None:
    restic = MagicMock()
    run(make_config(tmp_path), restic)
    restic.restore.assert_called_once_with()


def test_no_snapshots_exits_cleanly(tmp_path: Path) -> None:
    restic = MagicMock()
    restic.restore.side_effect = ResticNoSnapshotsError("none", 1, "no matching snapshot found")
    run(make_config(tmp_path), restic)  # must not raise


def test_restic_error_raises_system_exit(tmp_path: Path) -> None:
    restic = MagicMock()
    restic.restore.side_effect = ResticError("failed", 1, "connection refused")
    with pytest.raises(SystemExit):
        run(make_config(tmp_path), restic)


def test_sentinel_nested_path(tmp_path: Path) -> None:
    nested = tmp_path / "config" / "config.php"
    nested.parent.mkdir(parents=True)
    nested.touch()
    restic = MagicMock()
    run(make_config(tmp_path, sentinel="config/config.php"), restic)
    restic.restore.assert_not_called()
