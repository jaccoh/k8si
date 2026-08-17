"""Tests for backup (sidecar) mode."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from k8si.backend import BackupError, RepositoryNotInitializedError
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


def test_unrecoverable_backup_error_propagates(tmp_path: Path) -> None:
    """An unrecoverable backend failure must propagate out of _run_cycle so the
    container exits non-zero instead of silently reporting success (the Job's
    exit status is what the operator uses to set lastBackupResult)."""
    import pytest

    config = make_config(tmp_path)
    backend = MagicMock()
    backend.backup.side_effect = BackupError("failed", 1, "connection refused")
    with pytest.raises(BackupError):
        _run_cycle(config, backend)
    assert not (tmp_path / LAST_BACKUP_FILE).exists()


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


def test_auto_init_on_kopia_not_initialized_repo(tmp_path: Path) -> None:
    """Kopia reports a missing repository as a typed RepositoryNotInitializedError
    whose stderr holds kopia's own phrasing ("repository not initialized"), not the
    phrase "repository does not exist" — which only appears in the exception
    message. _run_cycle must detect the missing repo via the type, init, and retry."""
    config = make_config(tmp_path)
    backend = MagicMock()
    backend.backup.side_effect = [
        RepositoryNotInitializedError(
            "repository does not exist",
            1,
            "ERROR can't connect to storage: repository not initialized",
        ),
        None,  # succeeds after init
    ]
    _run_cycle(config, backend)
    backend.init.assert_called_once()
    assert backend.backup.call_count == 2
    assert (tmp_path / LAST_BACKUP_FILE).exists()


def test_forget_error_propagates(tmp_path: Path) -> None:
    """Forget/prune failure is a terminal failure: it must propagate so the
    container exits non-zero rather than reporting a false success.

    (Previously this test asserted the buggy swallow-and-return behavior —
    updated to match the fix in _run_cycle.)
    """
    import pytest

    config = make_config(tmp_path)
    backend = MagicMock()
    backend.forget.side_effect = BackupError("prune failed", 1, "other error")
    with pytest.raises(BackupError):
        _run_cycle(config, backend)
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


# ── spec.checkAfterBackup ─────────────────────────────────────────────────────


def test_check_called_when_run_check_true(tmp_path: Path) -> None:
    config = Config(
        mode="job",
        data_path=tmp_path,
        restic_repository="sftp:fake",
        restic_password="secret",
        restic_password_file=None,
        run_check=True,
    )
    backend = MagicMock()
    _run_cycle(config, backend)
    backend.check.assert_called_once()


def test_check_not_called_when_run_check_false(tmp_path: Path) -> None:
    config = make_config(tmp_path)  # run_check defaults to False
    backend = MagicMock()
    _run_cycle(config, backend)
    backend.check.assert_not_called()


def test_check_error_does_not_crash_sidecar(tmp_path: Path) -> None:
    config = Config(
        mode="job",
        data_path=tmp_path,
        restic_repository="sftp:fake",
        restic_password="secret",
        restic_password_file=None,
        run_check=True,
    )
    backend = MagicMock()
    backend.check.side_effect = BackupError("corrupted", 1, "data integrity error")
    _run_cycle(config, backend)  # must not raise


# ── additional _run_cycle edge cases ──────────────────────────────────────────


def test_backup_fails_after_init_is_logged_and_raises(tmp_path: Path) -> None:
    """When backup still fails after init, _run_cycle logs the error and re-raises
    (it must not report a successful cycle when the backup never succeeded).

    Previously this asserted a silent `return` — that was the bug: the container
    would exit 0 and the operator would record lastBackupResult=success.
    """
    import pytest

    config = make_config(tmp_path)
    backend = MagicMock()
    backend.backup.side_effect = [
        BackupError("failed", 1, "repository does not exist"),
        BackupError("failed", 1, "connection refused"),
    ]
    with pytest.raises(BackupError):
        _run_cycle(config, backend)
    backend.init.assert_called_once()
    assert not (tmp_path / LAST_BACKUP_FILE).exists()


def test_backup_fails_after_unlock_retry_is_logged_and_raises(tmp_path: Path) -> None:
    """When backup fails after unlock retry, _run_cycle logs the error and re-raises.

    Previously this asserted a silent `return` — updated to match the fix.
    """
    import pytest

    config = make_config(tmp_path)
    backend = MagicMock()
    backend.backup.side_effect = [
        BackupError("failed", 1, "repository is locked"),
        BackupError("failed", 1, "still locked"),
    ]
    with pytest.raises(BackupError):
        _run_cycle(config, backend)
    assert not (tmp_path / LAST_BACKUP_FILE).exists()


def test_forget_fails_after_unlock_retry_is_logged_and_raises(tmp_path: Path) -> None:
    """When forget fails after unlock retry, _run_cycle logs the error and re-raises.

    Previously this asserted a silent `return` — updated to match the fix.
    """
    import pytest

    config = make_config(tmp_path)
    backend = MagicMock()
    backend.forget.side_effect = [
        BackupError("prune failed", 1, "repository is locked"),
        BackupError("prune failed", 1, "still locked"),
    ]
    with pytest.raises(BackupError):
        _run_cycle(config, backend)
    assert not (tmp_path / LAST_BACKUP_FILE).exists()


def test_hook_stdout_is_logged(tmp_path: Path) -> None:
    """_run_hook() logs stdout when the hook produces output."""
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
        pre_snapshot_hook=tmp_path / "hook.sh",
    )
    backend = MagicMock()
    with patch("k8si.backup.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="hook output\n", stderr="")
        _run_cycle(config, backend)
    mock_run.assert_called_once()


def test_hook_failure_optional_logs_error_but_does_not_raise(tmp_path: Path) -> None:
    """A failing hook with required=False logs an error but does not raise."""
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
        pre_snapshot_hook=tmp_path / "hook.sh",
        pre_snapshot_hook_required=False,
    )
    backend = MagicMock()
    with patch("k8si.backup.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="script error")
        _run_cycle(config, backend)  # must not raise


def test_hook_failure_required_raises_runtime_error(tmp_path: Path) -> None:
    """A failing hook with required=True raises RuntimeError."""
    import pytest

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
        pre_snapshot_hook=tmp_path / "hook.sh",
        pre_snapshot_hook_required=True,
    )
    backend = MagicMock()
    with patch("k8si.backup.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="script error")
        with pytest.raises(RuntimeError, match="Pre-snapshot hook failed"):
            _run_cycle(config, backend)


def test_write_timestamp_oserror_is_swallowed(tmp_path: Path) -> None:
    """OSError during timestamp write is logged but does not propagate."""
    from k8si.backup import _write_last_backup_timestamp

    with patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
        _write_last_backup_timestamp(tmp_path)  # must not raise


# ── run() schedule loop ────────────────────────────────────────────────────────


def test_run_enters_loop_and_calls_cycle(tmp_path: Path) -> None:
    """run() computes next cron time, sleeps, then calls _run_cycle."""
    import pytest

    from k8si.backup import run

    config = make_config(tmp_path)
    backend = MagicMock()
    calls = {"n": 0}

    def fake_cycle(_cfg, _be):
        calls["n"] += 1
        raise SystemExit(0)

    with (
        patch("k8si.backup.time.sleep"),
        patch("k8si.backup._run_cycle", side_effect=fake_cycle),
    ):
        with pytest.raises(SystemExit):
            run(config, backend)

    assert calls["n"] == 1
