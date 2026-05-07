"""Tests for the restic wrapper."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from k8si.restic import Restic, ResticError, ResticNoSnapshotsError


def make_restic() -> Restic:
    return Restic(env={"RESTIC_REPOSITORY": "fake", "RESTIC_PASSWORD": "fake"})


def completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@patch("k8si.restic.subprocess.run")
def test_restore_success(mock_run: MagicMock) -> None:
    mock_run.return_value = completed(0)
    make_restic().restore()
    cmd = mock_run.call_args[0][0]
    assert "restore" in cmd
    assert "latest" in cmd
    assert "--target" in cmd


@patch("k8si.restic.subprocess.run")
def test_restore_raises_no_snapshots(mock_run: MagicMock) -> None:
    mock_run.return_value = completed(1, stderr="no matching snapshot found")
    with pytest.raises(ResticNoSnapshotsError):
        make_restic().restore()


@patch("k8si.restic.subprocess.run")
def test_restore_raises_generic_error(mock_run: MagicMock) -> None:
    mock_run.return_value = completed(1, stderr="connection refused")
    with pytest.raises(ResticError) as exc_info:
        make_restic().restore()
    assert not isinstance(exc_info.value, ResticNoSnapshotsError)


@patch("k8si.restic.subprocess.run")
def test_backup_passes_tags(mock_run: MagicMock) -> None:
    mock_run.return_value = completed(0)
    make_restic().backup(Path("/data"), tags=["app=sonarr", "env=prod"])
    cmd = mock_run.call_args[0][0]
    assert "--tag" in cmd
    assert "app=sonarr" in cmd


@patch("k8si.restic.subprocess.run")
def test_forget_includes_prune(mock_run: MagicMock) -> None:
    mock_run.return_value = completed(0)
    make_restic().forget(daily=7, weekly=4, monthly=3)
    cmd = mock_run.call_args[0][0]
    assert "--prune" in cmd
    assert "--keep-daily" in cmd
