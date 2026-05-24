"""Tests for the restic backend plugin (k8si.backends.restic)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import sh as _sh

from k8si.backend import BackupError, NoSnapshotsError
from k8si.backends.restic import ResticBackend


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_backend(
    env: dict[str, str] | None = None,
) -> tuple[ResticBackend, MagicMock]:
    """Return a ResticBackend with sh.restic.bake() mocked.

    The returned mock_cmd IS backend._r — set its return_value or side_effect
    to control what restic "returns" or "raises" in each test.
    """
    env = env or {"RESTIC_REPOSITORY": "fake", "RESTIC_PASSWORD": "secret"}
    with patch("k8si.backends.restic.sh") as mock_sh:
        mock_cmd = MagicMock()
        mock_sh.restic.bake.return_value = mock_cmd
        backend = ResticBackend(env=env)
    # backend._r = mock_cmd remains valid after the patch exits
    return backend, mock_cmd


def _sh_error(exit_code: int = 1, stderr: str = "error") -> _sh.ErrorReturnCode:
    """Create a sh.ErrorReturnCode subclass for testing."""
    cls = type(
        f"ErrorReturnCode_{exit_code}",
        (_sh.ErrorReturnCode,),
        {"exit_code": exit_code},
    )
    return cls("restic", b"", stderr.encode(), False)


# ── constructor ────────────────────────────────────────────────────────────────

def test_sftp_command_injected_as_global_opt() -> None:
    with patch("k8si.backends.restic.sh") as mock_sh:
        mock_sh.restic.bake.return_value = MagicMock()
        ResticBackend(env={
            "RESTIC_REPOSITORY": "fake",
            "RESTIC_PASSWORD": "secret",
            "RESTIC_SFTP_COMMAND": "ssh -i /key -p 23 host -s sftp",
        })
    bake_call = mock_sh.restic.bake.call_args
    args = bake_call[0]
    assert "-o" in args
    assert any("sftp.command=" in str(a) for a in args)


def test_no_sftp_command_no_global_opt() -> None:
    with patch("k8si.backends.restic.sh") as mock_sh:
        mock_sh.restic.bake.return_value = MagicMock()
        ResticBackend(env={"RESTIC_REPOSITORY": "fake", "RESTIC_PASSWORD": "secret"})
    bake_call = mock_sh.restic.bake.call_args
    args = bake_call[0]
    assert "-o" not in args


# ── init ───────────────────────────────────────────────────────────────────────

def test_init_calls_restic_init() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = "created restic repository"
    backend.init()
    mock_cmd.assert_called_once_with("init")


def test_init_raises_backup_error_on_failure() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.side_effect = _sh_error(1, "fatal: error")
    with pytest.raises(BackupError) as exc_info:
        backend.init()
    assert "fatal: error" in exc_info.value.stderr


# ── snapshots ──────────────────────────────────────────────────────────────────

def test_snapshots_returns_parsed_list() -> None:
    data = [{"id": "abc123", "short_id": "abc123", "time": "2026-05-07T19:00:00Z"}]
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = json.dumps(data)
    result = backend.snapshots()
    assert result == data
    mock_cmd.assert_called_once_with("snapshots", "--json")


def test_snapshots_passes_tag_filters() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = "[]"
    backend.snapshots(tags=["app=sonarr", "env=prod"])
    mock_cmd.assert_called_once_with(
        "snapshots", "--json", "--tag", "app=sonarr", "--tag", "env=prod"
    )


def test_snapshots_raises_on_transport_error() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.side_effect = _sh_error(
        1, "ssh: connect to host backup.example.com port 22: Connection refused"
    )
    with pytest.raises(BackupError) as exc_info:
        backend.snapshots()
    assert "Connection refused" in exc_info.value.stderr


def test_snapshots_returns_empty_on_empty_output() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = ""
    result = backend.snapshots()
    assert result == []


# ── ls ────────────────────────────────────────────────────────────────────────

def test_ls_returns_file_paths_only() -> None:
    jsonl = "\n".join([
        json.dumps({"message_type": "snapshot", "id": "abc"}),
        json.dumps({"type": "file", "path": "/data/config.xml", "name": "config.xml"}),
        json.dumps({"type": "dir",  "path": "/data/logs",       "name": "logs"}),
        json.dumps({"type": "file", "path": "/data/logs/app.log","name": "app.log"}),
    ])
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = jsonl
    paths = backend.ls("abc12345")
    assert "/data/config.xml" in paths
    assert "/data/logs/app.log" in paths
    assert "/data/logs" not in paths  # dirs excluded


def test_ls_passes_snapshot_id() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = ""
    backend.ls("deadbeef")
    mock_cmd.assert_called_once_with("ls", "--json", "deadbeef")


def test_ls_tolerates_malformed_json_lines() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = "not-json\n{bad}\n"
    result = backend.ls("snap1")
    assert result == []


# ── check_sentinels ───────────────────────────────────────────────────────────

def _popen_ctx(lines: list[str], returncode: int = 0) -> MagicMock:
    """Return a context-manager mock that yields lines from stdout."""
    proc = MagicMock()
    proc.stdout = iter(lines)
    proc.returncode = returncode
    proc.communicate.return_value = ("", "")
    ctx = MagicMock()
    ctx.__enter__.return_value = proc
    ctx.__exit__.return_value = False
    return ctx


def test_check_sentinels_finds_file_sentinel() -> None:
    backend, _ = _make_backend()
    lines = [
        json.dumps({"type": "file", "path": "/data/config.xml"}) + "\n",
        json.dumps({"type": "file", "path": "/data/other.txt"}) + "\n",
    ]
    with patch("subprocess.Popen", return_value=_popen_ctx(lines)):
        assert backend.check_sentinels("abc1234", ["config.xml"]) is True


def test_check_sentinels_finds_directory_sentinel() -> None:
    backend, _ = _make_backend()
    lines = [
        json.dumps({"type": "dir", "path": "/data/data/hoeve/files"}) + "\n",
    ]
    with patch("subprocess.Popen", return_value=_popen_ctx(lines)):
        assert backend.check_sentinels("abc1234", ["data/hoeve/files"]) is True


def test_check_sentinels_returns_false_when_missing() -> None:
    backend, _ = _make_backend()
    lines = [
        json.dumps({"type": "file", "path": "/data/other.txt"}) + "\n",
    ]
    with patch("subprocess.Popen", return_value=_popen_ctx(lines)):
        assert backend.check_sentinels("abc1234", ["config.xml"]) is False


def test_check_sentinels_raises_on_restic_error() -> None:
    backend, _ = _make_backend()
    lines: list[str] = []  # no output; restic exits non-zero
    with patch("subprocess.Popen", return_value=_popen_ctx(lines, returncode=1)):
        with pytest.raises(BackupError):
            backend.check_sentinels("abc1234", ["config.xml"])


def test_check_sentinels_empty_sentinels_returns_true() -> None:
    backend, _ = _make_backend()
    with patch("subprocess.Popen", return_value=_popen_ctx([])):
        assert backend.check_sentinels("abc1234", []) is True


# ── snapshot_size ─────────────────────────────────────────────────────────────

def test_snapshot_size_returns_total_bytes() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = json.dumps({"total_size": 8_198_041, "total_file_count": 42})
    size = backend.snapshot_size("abc12345")
    assert size == 8_198_041
    mock_cmd.assert_called_once_with("stats", "--json", "abc12345")


def test_snapshot_size_returns_zero_on_missing_key() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = json.dumps({})
    size = backend.snapshot_size("abc12345")
    assert size == 0


# ── restore ───────────────────────────────────────────────────────────────────

def test_restore_uses_latest_by_default() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = "restoring"
    backend.restore()
    mock_cmd.assert_called_once_with("restore", "latest", "--target", "/")


def test_restore_uses_specific_snapshot_id() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = "restoring"
    backend.restore(snapshot_id="abc12345")
    mock_cmd.assert_called_once_with("restore", "abc12345", "--target", "/")


def test_restore_raises_no_snapshots_error_on_no_match() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.side_effect = _sh_error(1, "no matching snapshot found")
    with pytest.raises(NoSnapshotsError):
        backend.restore()


def test_restore_raises_no_snapshots_error_on_no_snapshots_found() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.side_effect = _sh_error(1, "no snapshots found")
    with pytest.raises(NoSnapshotsError):
        backend.restore()


def test_restore_re_raises_other_backup_errors() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.side_effect = _sh_error(1, "connection refused")
    with pytest.raises(BackupError) as exc_info:
        backend.restore()
    assert not isinstance(exc_info.value, NoSnapshotsError)


# ── backup ────────────────────────────────────────────────────────────────────

def test_backup_passes_source_path() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = "snapshot saved"
    backend.backup(Path("/data"))
    mock_cmd.assert_called_once_with("backup", "/data")


def test_backup_passes_tags() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = "snapshot saved"
    backend.backup(Path("/data"), tags=["app=sonarr", "env=prod"])
    mock_cmd.assert_called_once_with(
        "backup", "/data", "--tag", "app=sonarr", "--tag", "env=prod"
    )


def test_backup_without_tags_omits_tag_flags() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = "snapshot saved"
    backend.backup(Path("/data"), tags=None)
    args = mock_cmd.call_args[0]
    assert "--tag" not in args


def test_backup_raises_backup_error_on_failure() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.side_effect = _sh_error(1, "locked by PID 99")
    with pytest.raises(BackupError) as exc_info:
        backend.backup(Path("/data"))
    assert "locked by PID 99" in exc_info.value.stderr


# ── forget ────────────────────────────────────────────────────────────────────

def test_forget_includes_prune_by_default() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = ""
    backend.forget(daily=7, weekly=4, monthly=3)
    args = mock_cmd.call_args[0]
    assert "--prune" in args
    assert "--keep-daily" in args
    assert "7" in args


def test_forget_omits_prune_when_false() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = ""
    backend.forget(daily=7, weekly=4, monthly=3, prune=False)
    args = mock_cmd.call_args[0]
    assert "--prune" not in args


def test_forget_passes_all_retention_values() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = ""
    backend.forget(daily=14, weekly=8, monthly=6)
    args = mock_cmd.call_args[0]
    assert "--keep-daily" in args
    assert "--keep-weekly" in args
    assert "--keep-monthly" in args
    assert "14" in args
    assert "8" in args
    assert "6" in args


# ── error conversion ───────────────────────────────────────────────────────────

def test_invoke_strips_stderr_whitespace() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.side_effect = _sh_error(1, "  trailing spaces  \n")
    with pytest.raises(BackupError) as exc_info:
        backend.init()
    assert exc_info.value.stderr == "trailing spaces"


def test_invoke_records_exit_code() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.side_effect = _sh_error(exit_code=3, stderr="weird exit")
    with pytest.raises(BackupError) as exc_info:
        backend.init()
    assert exc_info.value.returncode == 3
