"""Tests for backup (sidecar) mode."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from k8si.backend import BackupError
from k8si.backup import LAST_BACKUP_FILE, _run_cycle, run_once
from k8si.config import Config


def make_config(tmp_path: Path) -> Config:
    return Config(
        mode="backup",
        data_path=tmp_path,
        restic_repository="sftp:fake",
        restic_password="secret",
        restic_password_file=None,
        backup_schedule="0 * * * *",
        retention_daily=7,
        retention_weekly=4,
        retention_monthly=3,
        pre_snapshot_hook=None,
        backup_tags=["app=sonarr"],
    )


def test_successful_cycle_writes_timestamp(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    backend = MagicMock()
    _run_cycle(config, backend)
    assert (tmp_path / LAST_BACKUP_FILE).exists()
    backend.backup.assert_called_once_with(source=tmp_path, tags=["app=sonarr"])
    backend.forget.assert_called_once_with(daily=7, weekly=4, monthly=3, prune=True)


def test_backup_error_does_not_raise(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    backend = MagicMock()
    backend.backup.side_effect = BackupError("failed", 1, "connection refused")
    _run_cycle(config, backend)  # must not raise — sidecar keeps running


def test_run_once_runs_single_cycle(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    backend = MagicMock()
    run_once(config, backend)
    backend.backup.assert_called_once()
    backend.forget.assert_called_once()


def test_pre_snapshot_hook_called(tmp_path: Path) -> None:
    hook = tmp_path / "hook.sh"
    hook.write_text("#!/bin/sh\ntrue")
    hook.chmod(0o755)
    config = Config(
        mode="backup",
        data_path=tmp_path,
        restic_repository="sftp:fake",
        restic_password="secret",
        restic_password_file=None,
        backup_schedule="0 * * * *",
        retention_daily=7,
        retention_weekly=4,
        retention_monthly=3,
        pre_snapshot_hook=hook,
    )
    backend = MagicMock()
    with patch("k8si.backup.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _run_cycle(config, backend)
    mock_run.assert_called_once()


def test_auto_init_on_missing_repo(tmp_path: Path) -> None:
    """When backup fails with 'repository does not exist', init is called and backup retried."""
    config = make_config(tmp_path)
    backend = MagicMock()
    backend.backup.side_effect = [
        BackupError("failed", 1, "repository does not exist"),
        None,  # succeeds after init
    ]
    _run_cycle(config, backend)
    backend.init.assert_called_once()
    assert backend.backup.call_count == 2
    assert (tmp_path / LAST_BACKUP_FILE).exists()


def test_forget_error_does_not_raise(tmp_path: Path) -> None:
    """Forget/prune failure is logged but doesn't crash the sidecar."""
    config = make_config(tmp_path)
    backend = MagicMock()
    backend.forget.side_effect = BackupError("prune failed", 1, "other error")
    _run_cycle(config, backend)  # must not raise
    assert not (tmp_path / LAST_BACKUP_FILE).exists()


def test_backup_locked_causes_unlock_and_retry(tmp_path: Path) -> None:
    """When backup fails with locked error, unlock is called and backup retried."""
    config = make_config(tmp_path)
    backend = MagicMock()
    backend.backup.side_effect = [
        BackupError("failed", 1, "repository is locked by another process"),
        None,  # succeeds after unlock
    ]
    _run_cycle(config, backend)
    assert backend.unlock.call_count == 2  # proactive unlock + reactive unlock
    assert backend.backup.call_count == 2
    assert (tmp_path / LAST_BACKUP_FILE).exists()


def test_forget_locked_causes_unlock_and_retry(tmp_path: Path) -> None:
    """When forget fails with locked error, unlock is called and forget retried."""
    config = make_config(tmp_path)
    backend = MagicMock()
    backend.forget.side_effect = [
        BackupError("failed", 1, "repository is locked"),
        None,  # succeeds after unlock
    ]
    _run_cycle(config, backend)
    assert backend.unlock.call_count == 2  # proactive + reactive
    assert backend.forget.call_count == 2
    assert (tmp_path / LAST_BACKUP_FILE).exists()


def test_proactive_unlock_called_before_backup(tmp_path: Path) -> None:
    """unlock() is called proactively at the start of every cycle to release stale locks."""
    config = make_config(tmp_path)
    backend = MagicMock()
    call_order: list[str] = []
    backend.unlock.side_effect = lambda: call_order.append("unlock")
    backend.backup.side_effect = lambda **_: call_order.append("backup")
    _run_cycle(config, backend)
    assert call_order[0] == "unlock", f"Expected unlock first, got: {call_order}"
    assert "backup" in call_order


def test_proactive_unlock_failure_does_not_abort_backup(tmp_path: Path) -> None:
    """If proactive unlock raises, backup still proceeds."""
    config = make_config(tmp_path)
    backend = MagicMock()
    backend.unlock.side_effect = Exception("network error")
    _run_cycle(config, backend)
    backend.backup.assert_called_once()
