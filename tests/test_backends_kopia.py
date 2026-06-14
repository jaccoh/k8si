"""Tests for the Kopia backend plugin (k8si.backends.kopia)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import sh as _sh

from k8si.backends.kopia import KopiaBackend
from tests.helpers import popen_ctx as _popen_ctx

# ── helpers ────────────────────────────────────────────────────────────────────


def _make_backend(
    env: dict[str, str] | None = None,
) -> tuple[KopiaBackend, MagicMock]:
    """Return a KopiaBackend with sh.kopia.bake() mocked.

    The returned mock_cmd IS backend._k — set its return_value or side_effect
    to control what kopia "returns" or "raises" in each test.
    """
    env = env or {"RESTIC_REPOSITORY": "file:///tmp/kopia-repo", "RESTIC_PASSWORD": "secret"}
    with patch("k8si.backends.kopia.sh") as mock_sh:
        mock_cmd = MagicMock()
        mock_sh.kopia.bake.return_value = mock_cmd
        backend = KopiaBackend(env=env)
    return backend, mock_cmd


def _sh_error(exit_code: int = 1, stderr: str = "error") -> _sh.ErrorReturnCode:
    """Create a sh.ErrorReturnCode subclass for testing."""
    cls = type(
        f"ErrorReturnCode_{exit_code}",
        (_sh.ErrorReturnCode,),
        {"exit_code": exit_code},
    )
    return cls("kopia", b"", stderr.encode(), False)


# ── constructor & connect ──────────────────────────────────────────────────────


def test_ensure_connected_filesystem() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.side_effect = lambda *args, **kwargs: "[]" if "list" in args else "connected"

    # Trigger lazy connection check
    backend.snapshots()

    # Assert connection command called
    mock_cmd.assert_any_call("repository", "connect", "filesystem", "--path=/tmp/kopia-repo")


def test_ensure_connected_sftp() -> None:
    env = {
        "RESTIC_REPOSITORY": "sftp:u123@host.de:backup/app",
        "RESTIC_PASSWORD": "secret",
        "RESTIC_SFTP_COMMAND": "ssh -p 23 host",
    }
    backend, mock_cmd = _make_backend(env)
    mock_cmd.side_effect = lambda *args, **kwargs: "[]" if "list" in args else "connected"

    def mock_exists(path):
        if path == "/tmp/kopia.config":
            return False
        return True

    with patch("os.path.exists", side_effect=mock_exists):
        backend.snapshots()

    mock_cmd.assert_any_call(
        "repository",
        "connect",
        "sftp",
        "--host=host.de",
        "--username=u123",
        "--path=backup/app",
        "--port=23",
        "--keyfile=/restic-ssh/id_ed25519",
        "--known-hosts=/restic-ssh/known_hosts",
    )


# ── init ───────────────────────────────────────────────────────────────────────


def test_init_calls_repository_create() -> None:
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = "created"
    backend.init()
    mock_cmd.assert_called_once_with("repository", "create", "filesystem", "--path=/tmp/kopia-repo")


# ── snapshots ──────────────────────────────────────────────────────────────────


def test_snapshots_returns_mapped_list() -> None:
    kopia_data = [{"id": "snap-12345", "startTime": "2026-05-07T19:00:00Z"}]
    backend, mock_cmd = _make_backend()

    # Mock connection check success
    backend._connected = True
    mock_cmd.return_value = json.dumps(kopia_data)

    result = backend.snapshots()
    assert result == [{"id": "snap-12345", "short_id": "snap-123", "time": "2026-05-07T19:00:00Z"}]
    mock_cmd.assert_called_once_with("snapshot", "list", "--json")


# ── ls & sentinel check ────────────────────────────────────────────────────────


def test_ls_returns_paths() -> None:
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = "d /data/logs\nf /data/config.xml\n"

    paths = backend.ls("snap-12345")
    assert "/data/config.xml" in paths
    assert "/data/logs" in paths


def test_check_sentinels_finds_file() -> None:
    backend, _ = _make_backend()
    backend._connected = True
    lines = ["f /data/config.xml\n"]

    # Mock subprocess Popen
    proc = MagicMock()
    proc.stdout = iter(lines)
    proc.returncode = 0
    proc.communicate.return_value = ("", "")

    with patch("subprocess.Popen", return_value=MagicMock(__enter__=MagicMock(return_value=proc))):
        assert backend.check_sentinels("snap-123", ["config.xml"]) is True


# ── size ───────────────────────────────────────────────────────────────────────


def test_snapshot_size_returns_size() -> None:
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = json.dumps({"rootEntry": {"summ": {"size": 4242}}})

    size = backend.snapshot_size("snap-123")
    assert size == 4242
    mock_cmd.assert_called_once_with("snapshot", "show", "--json", "snap-123")


# ── restore & backup ───────────────────────────────────────────────────────────


def test_restore_calls_restore() -> None:
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = "restored"

    backend.restore("snap-123")
    mock_cmd.assert_called_once_with("snapshot", "restore", "snap-123", "/")


def test_backup_calls_snapshot_create() -> None:
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = "created"

    backend.backup(Path("/data"), tags=["app=test"])
    mock_cmd.assert_called_once_with("snapshot", "create", "/data", "--tags", "app=test")


# ── forget & unlock ────────────────────────────────────────────────────────────


def test_forget_sets_per_source_policy_and_maintenance() -> None:
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = ""

    # Simulate backup was called first
    backend._last_source = "/data"

    backend.forget(daily=7, weekly=4, monthly=3)
    mock_cmd.assert_any_call(
        "policy", "set", "/data", "--keep-daily=7", "--keep-weekly=4", "--keep-monthly=3"
    )
    mock_cmd.assert_any_call("maintenance", "run")


def test_forget_raises_when_backup_not_called_first() -> None:
    backend, mock_cmd = _make_backend()
    backend._connected = True

    # No prior backup() call — calling forget() without a source is a programming error
    import pytest

    with pytest.raises(ValueError, match="_last_source"):
        backend.forget(daily=7, weekly=4, monthly=3)


def test_unlock_runs_maintenance_force() -> None:
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = ""

    backend.unlock()
    mock_cmd.assert_called_once_with("maintenance", "run", "--force")


# ── check ─────────────────────────────────────────────────────────────────────


def test_check_calls_snapshot_verify_all() -> None:
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = ""

    backend.check()
    mock_cmd.assert_called_once_with("snapshot", "verify", "--all")


# ── _ensure_connected: non-initialization error is re-raised (line 65) ───────


def test_ensure_connected_reraises_non_initialization_error() -> None:
    """_ensure_connected() re-raises BackupError unchanged when not an init error."""
    from k8si.backend import BackupError

    backend, mock_cmd = _make_backend()
    mock_cmd.side_effect = _sh_error(1, "connection refused: network timeout")

    with patch("os.path.exists", return_value=False):
        try:
            backend._ensure_connected()
            raise AssertionError("Expected BackupError")
        except BackupError as e:
            assert "connection refused" in e.stderr


# ── _ensure_connected: config file already exists (lines 44-45) ───────────────


def test_ensure_connected_skips_when_config_file_exists() -> None:
    """_ensure_connected() uses cached config file instead of re-connecting."""
    backend, mock_cmd = _make_backend()
    mock_cmd.return_value = "[]"

    with (
        patch("os.path.exists", return_value=True),
        patch("os.path.getsize", return_value=100),
    ):
        backend.snapshots()

    # Connection command must NOT have been called (config file was present)
    for call in mock_cmd.call_args_list:
        assert call[0][0] != "repository", f"connect was called unexpectedly: {call}"


# ── _ensure_connected: repository not initialized error (lines 59-65) ─────────


def test_ensure_connected_raises_on_not_initialized() -> None:
    """_ensure_connected() re-raises as 'repository does not exist' when not initialized."""
    from k8si.backend import BackupError

    backend, mock_cmd = _make_backend()
    msg = "repository not initialized — run 'kopia repository create'"
    mock_cmd.side_effect = _sh_error(1, msg)

    try:
        backend.snapshots()
        raise AssertionError("Expected BackupError")
    except BackupError as e:
        assert "does not exist" in str(e)


# ── _parse_sftp_repo: malformed URL (line 71) ─────────────────────────────────


def test_parse_sftp_repo_malformed_url_raises() -> None:
    """Malformed SFTP repo URL raises BackupError."""
    from k8si.backend import BackupError

    backend, _ = _make_backend(env={"RESTIC_REPOSITORY": "sftp:BADURL", "RESTIC_PASSWORD": "s"})
    with patch("os.path.exists", return_value=False):
        try:
            backend._ensure_connected()
            raise AssertionError("Expected BackupError")
        except BackupError as e:
            assert "Malformed" in str(e)


# ── init: SFTP repo path (lines 99-100) ──────────────────────────────────────


def test_init_calls_repository_create_sftp() -> None:
    """init() uses sftp sub-command when repo starts with sftp:."""
    env = {"RESTIC_REPOSITORY": "sftp:u99@host.de:backup/data", "RESTIC_PASSWORD": "s"}
    backend, mock_cmd = _make_backend(env)
    mock_cmd.return_value = "created"

    with patch("os.path.exists", return_value=False):
        backend.init()

    args = mock_cmd.call_args[0]
    assert args[0] == "repository"
    assert args[1] == "create"
    assert "sftp" in args


# ── ls: empty lines skipped (line 132) ────────────────────────────────────────


def test_ls_skips_empty_lines() -> None:
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = "\n\n"
    assert backend.ls("snap-123") == []


# ── check_sentinels: various edge cases ───────────────────────────────────────


def test_check_sentinels_empty_sentinels_returns_true() -> None:
    backend, _ = _make_backend()
    backend._connected = True
    assert backend.check_sentinels("snap-123", []) is True


def test_check_sentinels_skips_empty_lines() -> None:
    backend, _ = _make_backend()
    backend._connected = True
    lines = ["\n", "f /data/config.xml\n"]
    with patch("subprocess.Popen", return_value=_popen_ctx(lines)):
        assert backend.check_sentinels("snap-123", ["config.xml"]) is True


def test_check_sentinels_skips_lines_with_no_parts() -> None:
    """Lines that are all spaces (strip → empty) are skipped."""
    backend, _ = _make_backend()
    backend._connected = True
    lines = ["   \n", "f /data/config.xml\n"]
    with patch("subprocess.Popen", return_value=_popen_ctx(lines)):
        assert backend.check_sentinels("snap-123", ["config.xml"]) is True


def test_check_sentinels_kills_process_when_all_found_early() -> None:
    """When all sentinels found before EOF, the kopia process is killed early."""
    backend, _ = _make_backend()
    backend._connected = True
    # First line satisfies the sentinel; second line triggers kill+break.
    lines = ["f /data/config.xml\n", "f /data/other.txt\n"]
    ctx = _popen_ctx(lines)
    with patch("subprocess.Popen", return_value=ctx):
        result = backend.check_sentinels("snap-123", ["config.xml"])
    assert result is True
    ctx.__enter__.return_value.kill.assert_called_once()


def test_check_sentinels_raises_on_kopia_error() -> None:
    from k8si.backend import BackupError

    backend, _ = _make_backend()
    backend._connected = True
    lines: list[str] = []
    with patch("subprocess.Popen", return_value=_popen_ctx(lines, returncode=1)):
        try:
            backend.check_sentinels("snap-123", ["config.xml"])
            raise AssertionError("Expected BackupError")
        except BackupError:
            pass


# ── snapshot_size: malformed JSON (lines 188-189) ─────────────────────────────


def test_snapshot_size_returns_zero_on_malformed_json() -> None:
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = "not-json"
    assert backend.snapshot_size("snap-123") == 0


# ── restore: not-found → NoSnapshotsError (lines 195-198) ────────────────────


def test_restore_raises_no_snapshots_on_not_found() -> None:
    from k8si.backend import NoSnapshotsError

    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.side_effect = _sh_error(1, "snapshot not found in repository")
    try:
        backend.restore("missing-snap")
        raise AssertionError("Expected NoSnapshotsError")
    except NoSnapshotsError:
        pass


# ── restore: non-"not found" error is re-raised (line 198) ──────────────────


def test_restore_reraises_non_not_found_error() -> None:
    """restore() re-raises BackupError unchanged when error is not 'not found'."""
    from k8si.backend import BackupError

    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.side_effect = _sh_error(1, "connection refused")

    try:
        backend.restore("snap-123")
        raise AssertionError("Expected BackupError")
    except BackupError as e:
        assert "connection refused" in e.stderr


# ── _invoke: sh.ErrorReturnCode (lines 246-252) ───────────────────────────────


def test_invoke_converts_error_return_code_to_backup_error() -> None:
    from k8si.backend import BackupError

    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.side_effect = _sh_error(2, "some kopia error")
    try:
        backend.check()
        raise AssertionError("Expected BackupError")
    except BackupError as e:
        assert e.returncode == 2
        assert "some kopia error" in e.stderr


# ── verify_snapshot ────────────────────────────────────────────────────────────


def test_verify_snapshot_returns_snapshot_info() -> None:
    from k8si.backend import BackupError, SnapshotInfo

    kopia_data = [
        {
            "id": "snap-abc12345",
            "startTime": "2026-06-14T10:00:00Z",
            "tags": {"k8si-run": "myrun-20260614"},
        }
    ]
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.side_effect = [
        json.dumps(kopia_data),
        json.dumps({"rootEntry": {"summ": {"size": 2048}}}),
    ]
    info = backend.verify_snapshot("k8si-run=myrun-20260614")
    assert isinstance(info, SnapshotInfo)
    assert info.id == "snap-abc12345"
    assert info.size_bytes == 2048


def test_verify_snapshot_raises_when_no_snapshot_found() -> None:
    from k8si.backend import BackupError

    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = "[]"
    with pytest.raises(BackupError, match="no snapshot found"):
        backend.verify_snapshot("k8si-run=norun")


def test_verify_snapshot_raises_on_ambiguous_match() -> None:
    from k8si.backend import BackupError

    kopia_data = [
        {"id": "snap-1", "startTime": "...", "tags": {"k8si-run": "myrun"}},
        {"id": "snap-2", "startTime": "...", "tags": {"k8si-run": "myrun"}},
    ]
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = json.dumps(kopia_data)
    with pytest.raises(BackupError, match="ambiguous"):
        backend.verify_snapshot("k8si-run=myrun")
