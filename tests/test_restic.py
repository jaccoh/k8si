"""Tests for the restic wrapper."""

import json
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
def test_restore_specific_snapshot(mock_run: MagicMock) -> None:
    mock_run.return_value = completed(0)
    make_restic().restore(snapshot_id="abc12345")
    cmd = mock_run.call_args[0][0]
    assert "abc12345" in cmd
    assert "latest" not in cmd


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


@patch("k8si.restic.subprocess.run")
def test_snapshots_returns_list(mock_run: MagicMock) -> None:
    data = [{"id": "abc", "short_id": "abc12345", "time": "2026-05-07T19:00:00Z"}]
    mock_run.return_value = completed(0, stdout=json.dumps(data))
    result = make_restic().snapshots()
    assert result == data
    cmd = mock_run.call_args[0][0]
    assert "snapshots" in cmd
    assert "--json" in cmd


@patch("k8si.restic.subprocess.run")
def test_snapshots_filters_by_tags(mock_run: MagicMock) -> None:
    mock_run.return_value = completed(0, stdout="[]")
    make_restic().snapshots(tags=["app=prowlarr"])
    cmd = mock_run.call_args[0][0]
    assert "--tag" in cmd
    assert "app=prowlarr" in cmd


@patch("k8si.restic.subprocess.run")
def test_snapshots_returns_empty_on_error(mock_run: MagicMock) -> None:
    mock_run.return_value = completed(1, stderr="repo not found")
    result = make_restic().snapshots()
    assert result == []


@patch("k8si.restic.subprocess.run")
def test_ls_parses_file_paths(mock_run: MagicMock) -> None:
    jsonl = "\n".join([
        json.dumps({"message_type": "snapshot", "id": "abc"}),
        json.dumps({"type": "file", "path": "/data/config.xml", "name": "config.xml"}),
        json.dumps({"type": "dir", "path": "/data/logs", "name": "logs"}),
        json.dumps({"type": "file", "path": "/data/logs/app.log", "name": "app.log"}),
    ])
    mock_run.return_value = completed(0, stdout=jsonl)
    paths = make_restic().ls("abc12345")
    assert "/data/config.xml" in paths
    assert "/data/logs/app.log" in paths
    assert "/data/logs" not in paths  # dirs excluded


@patch("k8si.restic.subprocess.run")
def test_snapshot_size_returns_bytes(mock_run: MagicMock) -> None:
    mock_run.return_value = completed(0, stdout=json.dumps({"total_size": 8_198_041}))
    size = make_restic().snapshot_size("abc12345")
    assert size == 8_198_041
    cmd = mock_run.call_args[0][0]
    assert "stats" in cmd
    assert "abc12345" in cmd
