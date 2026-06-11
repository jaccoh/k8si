"""Tests for Config env var parsing."""

import pytest

from k8si.config import Config


def test_backup_name_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "restore")
    monkeypatch.setenv("RESTIC_REPOSITORY", "sftp:fake")
    monkeypatch.setenv("RESTIC_PASSWORD", "secret")
    monkeypatch.setenv("K8SI_BACKUP_NAME", "my-backup")
    monkeypatch.setenv("K8SI_BACKUP_NAMESPACE", "my-ns")
    config = Config.from_env()
    assert config.backup_name == "my-backup"
    assert config.backup_namespace == "my-ns"


def test_backup_name_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "restore")
    monkeypatch.setenv("RESTIC_REPOSITORY", "sftp:fake")
    monkeypatch.setenv("RESTIC_PASSWORD", "secret")
    monkeypatch.delenv("K8SI_BACKUP_NAME", raising=False)
    monkeypatch.delenv("K8SI_BACKUP_NAMESPACE", raising=False)
    config = Config.from_env()
    assert config.backup_name is None
    assert config.backup_namespace is None
