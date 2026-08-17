"""BackupBackend protocol — swap restic for kopia (or anything) without touching callers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


class BackupError(Exception):
    def __init__(self, message: str, returncode: int = 1, stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class NoSnapshotsError(BackupError):
    pass


class RepositoryNotInitializedError(BackupError):
    """The repository does not exist yet — recoverable by backend.init()."""


@dataclass
class SnapshotInfo:
    id: str
    short_id: str
    size_bytes: int


@runtime_checkable
class BackupBackend(Protocol):
    def init(self) -> None: ...
    def snapshots(self, tags: list[str] | None = None) -> list[dict]: ...
    def ls(self, snapshot_id: str) -> list[str]: ...
    def check_sentinels(self, snapshot_id: str, sentinels: list[str]) -> bool: ...
    def snapshot_size(self, snapshot_id: str) -> int: ...
    def restore(self, snapshot_id: str = "latest") -> None: ...
    def backup(self, source: Path, tags: list[str] | None = None) -> None: ...
    def forget(self, daily: int, weekly: int, monthly: int, prune: bool = True) -> None: ...
    def unlock(self) -> None: ...
    def check(self) -> None: ...
    def verify_snapshot(self, run_tag: str) -> SnapshotInfo: ...
