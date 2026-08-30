"""Tests for the Kopia backend plugin (k8si.backends.kopia)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
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
    env = env or {"RESTIC_REPOSITORY": "local:/tmp/kopia-repo", "RESTIC_PASSWORD": "secret"}
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
    assert result == [
        {"id": "snap-12345", "short_id": "snap-12345", "time": "2026-05-07T19:00:00Z"}
    ]
    mock_cmd.assert_called_once_with("snapshot", "list", "--json")


def test_snapshots_short_id_is_full_id() -> None:
    """short_id must be the FULL manifest ID for kopia: restore.py restores via
    snapshots()[-1]['short_id'], and kopia (unlike restic) does not resolve
    restic-style 8-character ID prefixes."""
    kopia_data = [{"id": "abcdef1234567890abcdef1234567890", "startTime": "2026-05-07T19:00:00Z"}]
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = json.dumps(kopia_data)

    result = backend.snapshots()
    assert result[0]["short_id"] == "abcdef1234567890abcdef1234567890"


def test_snapshots_filters_tags_with_kopia_prefix() -> None:
    """kopia returns user tags under a 'tag:' prefix ({"tag:app": "kopia-e2e"});
    the restic-style filter ["app=kopia-e2e"] must still select those snapshots."""
    kopia_data = [
        {"id": "snap-1", "startTime": "2026-05-07T19:00:00Z", "tags": {"tag:app": "kopia-e2e"}},
        {"id": "snap-2", "startTime": "2026-05-07T20:00:00Z", "tags": {"tag:app": "other"}},
    ]
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = json.dumps(kopia_data)

    result = backend.snapshots(tags=["app=kopia-e2e"])
    assert [s["id"] for s in result] == ["snap-1"]


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


def test_check_sentinels_matches_real_kopia_ls_format() -> None:
    """Real `kopia ls -r <id>` (0.15.0) prefixes every path with the snapshot ID:
    'b877df1fcfbf22801f741bef43d11bd0/sentinel.txt' — must still match sentinel
    'sentinel.txt'."""
    backend, _ = _make_backend()
    backend._connected = True
    lines = [
        "b877df1fcfbf22801f741bef43d11bd0/payload.txt\n",
        "b877df1fcfbf22801f741bef43d11bd0/sentinel.txt\n",
    ]

    with patch("subprocess.Popen", return_value=_popen_ctx(lines)):
        assert backend.check_sentinels("b877df1fcfbf22801f741bef43d11bd0", ["sentinel.txt"]) is True


# ── size ───────────────────────────────────────────────────────────────────────


def test_snapshot_size_returns_size() -> None:
    backend, mock_cmd = _make_backend()
    backend._connected = True
    # kopia 0.15.0 has no `snapshot show`; `snapshot list --json` carries
    # rootEntry.summ.size per manifest.
    mock_cmd.return_value = json.dumps(
        [
            {"id": "snap-other", "rootEntry": {"summ": {"size": 1}}},
            {"id": "snap-123", "rootEntry": {"summ": {"size": 4242}}},
        ]
    )

    size = backend.snapshot_size("snap-123")
    assert size == 4242
    mock_cmd.assert_called_once_with("snapshot", "list", "--json")


def test_snapshot_size_unknown_id_returns_zero() -> None:
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = json.dumps([{"id": "snap-other", "rootEntry": {"summ": {"size": 1}}}])

    assert backend.snapshot_size("snap-123") == 0


# ── restore & backup ───────────────────────────────────────────────────────────


def test_restore_calls_restore() -> None:
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = "restored"

    backend.restore("snap-123")
    # kopia restores the snapshot CONTENTS into the target dir (unlike restic,
    # which recreates the absolute source path under --target), so the target
    # must be DATA_PATH — restoring to "/" would scatter files across the rootfs.
    mock_cmd.assert_called_once_with("snapshot", "restore", "snap-123", "/data")


def test_restore_targets_data_path_from_env() -> None:
    backend, mock_cmd = _make_backend(
        env={
            "RESTIC_REPOSITORY": "local:/tmp/kopia-repo",
            "RESTIC_PASSWORD": "secret",
            "DATA_PATH": "/custom/data",
        }
    )
    backend._connected = True
    mock_cmd.return_value = "restored"

    backend.restore("snap-123")
    mock_cmd.assert_called_once_with("snapshot", "restore", "snap-123", "/custom/data")


def test_backup_calls_snapshot_create() -> None:
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = "created"

    backend.backup(Path("/data"), tags=["app=test"])
    mock_cmd.assert_called_once_with("snapshot", "create", "/data", "--tags", "app:test", "--json")


def test_backup_translates_restic_style_tags() -> None:
    """Callers speak restic-style 'key=value' tags (CRD spec.tags, BACKUP_TAGS);
    kopia requires 'key:value' — the backend must translate, not pass through."""
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = "created"

    backend.backup(Path("/data"), tags=["app=sonarr", "env=prod"])
    mock_cmd.assert_called_once_with(
        "snapshot",
        "create",
        "/data",
        "--tags",
        "app:sonarr",
        "--tags",
        "env:prod",
        "--json",
    )


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


def test_check_calls_snapshot_verify_all_sources() -> None:
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = ""

    backend.check()
    # kopia 0.15.0 has no --all flag; all sources are selected via --sources=all
    mock_cmd.assert_called_once_with("snapshot", "verify", "--sources=all")


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
    """_ensure_connected() raises the typed RepositoryNotInitializedError so the
    backup cycle can auto-init without string-matching kopia's stderr phrasing
    ("repository does not exist" never appears in kopia's own stderr)."""
    from k8si.backend import RepositoryNotInitializedError

    backend, mock_cmd = _make_backend()
    msg = "repository not initialized — run 'kopia repository create'"
    mock_cmd.side_effect = _sh_error(1, msg)

    with patch("os.path.exists", return_value=False):
        with pytest.raises(RepositoryNotInitializedError, match="does not exist"):
            backend.snapshots()


def test_ensure_connected_not_initialized_phrase_in_stdout() -> None:
    """With _err_to_out=True, sh merges kopia's error output into STDOUT and
    e.stderr stays empty — the not-initialized detection must read both."""
    from k8si.backend import RepositoryNotInitializedError

    backend, mock_cmd = _make_backend()
    # same construction as _sh_error, but the kopia text rides in STDOUT
    cls = type("ErrorReturnCode_1", (_sh.ErrorReturnCode,), {"exit_code": 1})
    mock_cmd.side_effect = cls(
        "kopia", b"repository not initialized in the provided storage", b"", False
    )

    with patch("os.path.exists", return_value=False):
        with pytest.raises(RepositoryNotInitializedError):
            backend.snapshots()


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


def test_invoke_returns_and_logs_merged_output(caplog) -> None:  # type: ignore[no-untyped-def]
    """kopia writes 'snapshot create' output (including the 'Created snapshot
    ... and ID ...' artifact line and progress) to STDERR — the backend bakes
    _err_to_out=True so the merged stream is returned AND re-logged into the
    pod log that _parse_artifact parses."""
    import logging as _logging

    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = (
        "Snapshotting root@host:/data ...\n"
        "Created snapshot with root k1 and ID snap-full-id in 0s\n"
    )

    with caplog.at_level(_logging.INFO, logger="k8si.backends.kopia"):
        returned = backend._invoke("snapshot", "create", "/data")

    assert "Created snapshot with root k1 and ID snap-full-id" in returned
    joined = "\n".join(r.message for r in caplog.records)
    assert "Created snapshot with root k1 and ID snap-full-id in 0s" in joined


def test_snapshots_parses_json_among_interleaved_log_lines() -> None:
    """With stderr merged into stdout, kopia log noise may surround the JSON —
    snapshots() must still parse the array."""
    raw = (
        "kopia: some progress line\n"
        '[{"id": "snap-1", "startTime": "2026-05-07T19:00:00Z"}]\n'
        "kopia: trailing log\n"
    )
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = raw

    result = backend.snapshots()
    assert result == [{"id": "snap-1", "short_id": "snap-1", "time": "2026-05-07T19:00:00Z"}]


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
    from k8si.backend import SnapshotInfo

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
        json.dumps([{"id": "snap-abc12345", "rootEntry": {"summ": {"size": 2048}}}]),
    ]
    info = backend.verify_snapshot("k8si-run=myrun-20260614")
    assert isinstance(info, SnapshotInfo)
    assert info.id == "snap-abc12345"
    assert info.short_id == "snap-abc12345"
    assert info.size_bytes == 2048


def test_verify_snapshot_matches_kopia_prefixed_tags() -> None:
    """kopia prefixes user tags with 'tag:' in snapshot list JSON — verify_snapshot
    must find 'k8si-run=myrun' in {"tag:k8si-run": "myrun"}."""
    from k8si.backend import SnapshotInfo

    kopia_data = [
        {
            "id": "snap-abc12345",
            "startTime": "2026-06-14T10:00:00Z",
            "tags": {"tag:k8si-run": "myrun-20260614"},
        }
    ]
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.side_effect = [
        json.dumps(kopia_data),
        json.dumps([{"id": "snap-abc12345", "rootEntry": {"summ": {"size": 2048}}}]),
    ]
    info = backend.verify_snapshot("k8si-run=myrun-20260614")
    assert isinstance(info, SnapshotInfo)
    assert info.id == "snap-abc12345"


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


# ── snapshot create --json: structured artifact capture ───────────────────────

# What kopia 0.15.0 prints for `snapshot create --json`: the snapshot manifest,
# indented, to stdout — with progress (stderr) merged in by _err_to_out.
KOPIA_CREATE_JSON_OUTPUT = """\
 * 0 hashing, 0 hashed (0 B), 0 cached (0 B), uploaded 0 B, estimating...
 * 0 hashing, 2 hashed (14 B), 0 cached (0 B), uploaded 195 B, estimating...
{
  "id": "5fc52d496b7a5c7866fd6ca1f9d8d2c2",
  "source": {
    "host": "k8si-job",
    "userName": "root",
    "path": "/data"
  },
  "startTime": "2026-08-30T20:00:00.000000000Z",
  "endTime": "2026-08-30T20:00:01.000000000Z",
  "rootEntry": {
    "name": "data",
    "type": "DIR",
    "obj": "ked2772c42cdaf458bedc3aa8ef5b5e6d",
    "summ": {
      "size": 14
    }
  }
}
"""


def test_backup_passes_json_flag() -> None:
    """kopia 0.15.0 supports `snapshot create --json` (manifest to stdout) —
    the backend must request it so the artifact comes from structured output."""
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = "created"

    backend.backup(Path("/data"), tags=["app=test"])
    mock_cmd.assert_called_once_with("snapshot", "create", "/data", "--tags", "app:test", "--json")


def test_backup_parses_manifest_into_last_snapshot() -> None:
    """The manifest printed by --json is captured as the artifact: snapshot id
    plus total size from rootEntry.summ.size."""
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = KOPIA_CREATE_JSON_OUTPUT

    backend.backup(Path("/data"))

    assert backend.last_snapshot == {
        "snapshotId": "5fc52d496b7a5c7866fd6ca1f9d8d2c2",
        "sizeBytes": 14,
    }


def test_backup_without_manifest_leaves_last_snapshot_none() -> None:
    """No manifest in the output (older kopia, or --json ignored) → no capture;
    the list-based fallback resolves the artifact instead."""
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = "Created snapshot with root k1 and ID abc in 1s"

    backend.backup(Path("/data"))

    assert backend.last_snapshot is None


def test_backup_manifest_without_size_keeps_none_size() -> None:
    """A manifest without rootEntry.summ.size (partial snapshot) yields
    sizeBytes None rather than a bogus 0."""
    backend, mock_cmd = _make_backend()
    backend._connected = True
    mock_cmd.return_value = '{\n  "id": "snap1",\n  "startTime": "t",\n  "rootEntry": {}\n}\n'

    backend.backup(Path("/data"))

    assert backend.last_snapshot == {"snapshotId": "snap1", "sizeBytes": None}


# ── typed repo-locked detection ───────────────────────────────────────────────


def test_invoke_locked_output_raises_typed_error() -> None:
    """A repo-locked kopia failure must surface as RepositoryLockedError so the
    retry logic can catch the type instead of sniffing stderr strings."""
    from k8si.backend import RepositoryLockedError

    backend, mock_cmd = _make_backend()
    err = _sh_error(1, "ERROR can't lock repository: locked by another client")
    mock_cmd.side_effect = err

    with pytest.raises(RepositoryLockedError) as exc_info:
        backend._invoke("snapshot", "create", "/data")

    assert str(exc_info.value) == "kopia exited 1"
    assert "lock" in exc_info.value.stderr


def test_invoke_unlocked_output_raises_plain_backup_error() -> None:
    from k8si.backend import BackupError, RepositoryLockedError

    backend, mock_cmd = _make_backend()
    mock_cmd.side_effect = _sh_error(1, "ERROR unable to connect")

    with pytest.raises(BackupError) as exc_info:
        backend._invoke("snapshot", "create", "/data")

    assert not isinstance(exc_info.value, RepositoryLockedError)
