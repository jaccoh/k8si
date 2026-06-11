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


def test_backup_mode_reads_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "backup")
    monkeypatch.setenv("RESTIC_REPOSITORY", "sftp:fake")
    monkeypatch.setenv("RESTIC_PASSWORD", "secret")
    monkeypatch.setenv("BACKUP_SCHEDULE", "0 2 * * *")
    config = Config.from_env()
    assert config.mode == "backup"
    assert config.backup_schedule == "0 2 * * *"


def test_backup_mode_reads_pre_snapshot_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "backup")
    monkeypatch.setenv("RESTIC_REPOSITORY", "sftp:fake")
    monkeypatch.setenv("RESTIC_PASSWORD", "secret")
    monkeypatch.setenv("BACKUP_SCHEDULE", "0 2 * * *")
    monkeypatch.setenv("PRE_SNAPSHOT_HOOK", "/hooks/pre.sh")
    monkeypatch.setenv("PRE_SNAPSHOT_HOOK_REQUIRED", "true")
    config = Config.from_env()
    assert str(config.pre_snapshot_hook) == "/hooks/pre.sh"
    assert config.pre_snapshot_hook_required is True


def test_job_mode_no_schedule_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "job")
    monkeypatch.setenv("RESTIC_REPOSITORY", "sftp:fake")
    monkeypatch.setenv("RESTIC_PASSWORD", "secret")
    config = Config.from_env()
    assert config.mode == "job"
    assert config.backup_schedule is None


def test_backup_tags_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "job")
    monkeypatch.setenv("RESTIC_REPOSITORY", "sftp:fake")
    monkeypatch.setenv("RESTIC_PASSWORD", "secret")
    monkeypatch.setenv("BACKUP_TAGS", "env=prod, team=backend")
    config = Config.from_env()
    assert config.backup_tags == ["env=prod", "team=backend"]


def test_run_check_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "job")
    monkeypatch.setenv("RESTIC_REPOSITORY", "sftp:fake")
    monkeypatch.setenv("RESTIC_PASSWORD", "secret")
    monkeypatch.setenv("RUN_CHECK", "true")
    config = Config.from_env()
    assert config.run_check is True


def test_require_raises_config_error_on_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from k8si.config import ConfigError, _require

    monkeypatch.delenv("MISSING_VAR", raising=False)
    with pytest.raises(ConfigError, match="MISSING_VAR"):
        _require("MISSING_VAR", "test variable")


def test_parse_duration_hours_days(monkeypatch: pytest.MonkeyPatch) -> None:
    from k8si.config import _parse_duration_hours

    assert _parse_duration_hours("7d") == 168.0
    assert _parse_duration_hours("1d") == 24.0


def test_parse_duration_hours_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    from k8si.config import _parse_duration_hours

    assert _parse_duration_hours("48h") == 48.0
    assert _parse_duration_hours("1h") == 1.0


def test_parse_duration_hours_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    from k8si.config import _parse_duration_hours

    assert _parse_duration_hours("30m") == pytest.approx(0.5)


def test_parse_duration_hours_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from k8si.config import ConfigError, _parse_duration_hours

    with pytest.raises(ConfigError):
        _parse_duration_hours("5x")


def test_parse_bytes_mebibytes() -> None:
    from k8si.config import _parse_bytes

    assert _parse_bytes("1Mi") == 1024**2
    assert _parse_bytes("2Gi") == 2 * 1024**3


def test_parse_bytes_megabytes() -> None:
    from k8si.config import _parse_bytes

    assert _parse_bytes("1M") == 1_000_000
    assert _parse_bytes("1G") == 1_000_000_000


def test_parse_bytes_kibibytes() -> None:
    from k8si.config import _parse_bytes

    assert _parse_bytes("4Ki") == 4096
    assert _parse_bytes("1K") == 1000


def test_parse_bytes_tebibytes() -> None:
    from k8si.config import _parse_bytes

    assert _parse_bytes("1Ti") == 1024**4
    assert _parse_bytes("1T") == 1000**4


def test_parse_bytes_raw_integer() -> None:
    from k8si.config import _parse_bytes

    assert _parse_bytes("1048576") == 1048576


def test_invalid_mode_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from k8si.config import ConfigError

    monkeypatch.setenv("MODE", "bogus")
    monkeypatch.setenv("RESTIC_REPOSITORY", "sftp:fake")
    monkeypatch.setenv("RESTIC_PASSWORD", "secret")
    with pytest.raises(ConfigError, match="MODE must be"):
        Config.from_env()


def test_invalid_backend_type_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from k8si.config import ConfigError

    monkeypatch.setenv("MODE", "job")
    monkeypatch.setenv("RESTIC_REPOSITORY", "sftp:fake")
    monkeypatch.setenv("RESTIC_PASSWORD", "secret")
    monkeypatch.setenv("BACKEND_TYPE", "s3compat")
    with pytest.raises(ConfigError, match="BACKEND_TYPE must be"):
        Config.from_env()


def test_missing_password_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from k8si.config import ConfigError

    monkeypatch.setenv("MODE", "restore")
    monkeypatch.setenv("RESTIC_REPOSITORY", "sftp:fake")
    monkeypatch.delenv("RESTIC_PASSWORD", raising=False)
    monkeypatch.delenv("RESTIC_PASSWORD_FILE", raising=False)
    with pytest.raises(ConfigError, match="RESTIC_PASSWORD"):
        Config.from_env()


def test_restore_sentinels_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "restore")
    monkeypatch.setenv("RESTIC_REPOSITORY", "sftp:fake")
    monkeypatch.setenv("RESTIC_PASSWORD", "secret")
    monkeypatch.setenv("RESTORE_SENTINELS", "/data/ready, /data/ok")
    config = Config.from_env()
    assert config.restore_sentinels == ["/data/ready", "/data/ok"]


def test_restore_size_min_max_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "restore")
    monkeypatch.setenv("RESTIC_REPOSITORY", "sftp:fake")
    monkeypatch.setenv("RESTIC_PASSWORD", "secret")
    monkeypatch.setenv("RESTORE_SIZE_MIN", "1Mi")
    monkeypatch.setenv("RESTORE_SIZE_MAX", "10Gi")
    config = Config.from_env()
    assert config.restore_size_min == 1024**2
    assert config.restore_size_max == 10 * 1024**3


def test_restore_max_age_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "restore")
    monkeypatch.setenv("RESTIC_REPOSITORY", "sftp:fake")
    monkeypatch.setenv("RESTIC_PASSWORD", "secret")
    monkeypatch.setenv("RESTORE_MAX_AGE", "7d")
    config = Config.from_env()
    assert config.restore_max_age_hours == 168.0
