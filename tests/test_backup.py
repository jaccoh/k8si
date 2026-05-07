"""Tests for backup (sidecar) mode."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from k8si.backup import LAST_BACKUP_FILE, _run_cycle
from k8si.config import Config
from k8si.restic import ResticError


def make_config(tmp_path: Path) -> Config:
    return Config(
        mode="backup",
        data_path=tmp_path,
        restic_repository="sftp:fake",
        restic_password="secret",
        restic_password_file=None,
        sentinel_file=None,
        backup_schedule="0 * * * *",
        retention_daily=7,
        retention_weekly=4,
        retention_monthly=3,
        pre_backup_hook=None,
        backup_tags=["app=sonarr"],
    )


def test_successful_cycle_writes_timestamp(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    restic = MagicMock()
    _run_cycle(config, restic)
    assert (tmp_path / LAST_BACKUP_FILE).exists()
    restic.backup.assert_called_once_with(source=tmp_path, tags=["app=sonarr"])
    restic.forget.assert_called_once_with(daily=7, weekly=4, monthly=3, prune=True)


def test_backup_error_does_not_raise(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    restic = MagicMock()
    restic.backup.side_effect = ResticError("failed", 1, "connection refused")
    _run_cycle(config, restic)  # must not raise — sidecar keeps running


def test_pre_backup_hook_called(tmp_path: Path) -> None:
    hook = tmp_path / "hook.sh"
    hook.write_text("#!/bin/sh\ntrue")
    hook.chmod(0o755)
    config = Config(
        mode="backup",
        data_path=tmp_path,
        restic_repository="sftp:fake",
        restic_password="secret",
        restic_password_file=None,
        sentinel_file=None,
        backup_schedule="0 * * * *",
        retention_daily=7,
        retention_weekly=4,
        retention_monthly=3,
        pre_backup_hook=hook,
    )
    restic = MagicMock()
    with patch("k8si.backup.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _run_cycle(config, restic)
    mock_run.assert_called_once()
