"""Tests for the Kopia backend plugin (k8si.backends.kopia)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import sh as _sh

from k8si.backends.kopia import KopiaBackend

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


def test_forget_sets_policy_and_maintenance() -> None:
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = ""

    backend.forget(daily=7, weekly=4, monthly=3)
    mock_cmd.assert_any_call(
        "policy", "set", "--global", "--keep-daily=7", "--keep-weekly=4", "--keep-monthly=3"
    )
    mock_cmd.assert_any_call("maintenance", "run")


def test_unlock_runs_maintenance_force() -> None:
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = ""

    backend.unlock()
    mock_cmd.assert_called_once_with("maintenance", "run", "--force")
