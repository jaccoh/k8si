"""Tests for the BackupBackend protocol and shared exception types."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from k8si.backend import BackupBackend, BackupError, NoSnapshotsError
from k8si.backends.restic import ResticBackend

# ── exception hierarchy ────────────────────────────────────────────────────────


def test_backup_error_stores_returncode_and_stderr() -> None:
    err = BackupError("something failed", returncode=2, stderr="disk full")
    assert err.returncode == 2
    assert err.stderr == "disk full"
    assert str(err) == "something failed"


def test_no_snapshots_error_is_subclass_of_backup_error() -> None:
    err = NoSnapshotsError("no snapshots", returncode=1, stderr="")
    assert isinstance(err, BackupError)


def test_backup_error_default_returncode() -> None:
    err = BackupError("oops")
    assert err.returncode == 1
    assert err.stderr == ""


# ── protocol conformance ───────────────────────────────────────────────────────


def test_backup_backend_is_runtime_checkable() -> None:
    """Protocol is tagged @runtime_checkable so isinstance works."""
    mock = MagicMock(spec=BackupBackend)
    assert isinstance(mock, BackupBackend)


def test_restic_backend_satisfies_protocol() -> None:
    """ResticBackend structurally satisfies BackupBackend without inheritance."""
    with patch("k8si.backends.restic.sh") as mock_sh:
        mock_sh.restic.bake.return_value = MagicMock()
        backend = ResticBackend(env={"RESTIC_REPOSITORY": "fake", "RESTIC_PASSWORD": "x"})
    assert isinstance(backend, BackupBackend)


def test_protocol_requires_all_methods() -> None:
    """An object missing a protocol method does not satisfy BackupBackend."""

    class Incomplete:
        def init(self) -> None: ...

        # missing snapshots, ls, snapshot_size, restore, backup, forget

    assert not isinstance(Incomplete(), BackupBackend)


def test_protocol_satisfied_by_duck_typed_class() -> None:
    """Any class with all methods satisfies the protocol, no inheritance needed."""

    class FakeBackend:
        def init(self) -> None: ...
        def snapshots(self, tags=None):
            return []

        def ls(self, snapshot_id: str):
            return []

        def check_sentinels(self, snapshot_id: str, sentinels: list) -> bool:
            return True

        def snapshot_size(self, snapshot_id: str):
            return 0

        def restore(self, snapshot_id: str = "latest") -> None: ...
        def backup(self, source: Path, tags=None) -> None: ...
        def forget(self, daily: int, weekly: int, monthly: int, prune: bool = True) -> None: ...
        def unlock(self) -> None: ...

    assert isinstance(FakeBackend(), BackupBackend)
