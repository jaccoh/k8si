"""Tests for k8si/cli.py — main() dispatch and _build_backend_env()."""

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# _build_backend_env helpers
# ---------------------------------------------------------------------------


def _make_config(**kwargs) -> MagicMock:
    cfg = MagicMock()
    cfg.restic_repository = kwargs.get("restic_repository", "sftp:host:/repo")
    cfg.restic_password = kwargs.get("restic_password", "secret")
    cfg.restic_password_file = kwargs.get("restic_password_file", None)
    cfg.backend_type = kwargs.get("backend_type", "restic")
    cfg.mode = kwargs.get("mode", "restore")
    return cfg


def test_build_backend_env_sets_repository() -> None:
    from k8si.cli import _build_backend_env

    cfg = _make_config(restic_repository="sftp:host:/myrepo")
    env = _build_backend_env(cfg)
    assert env["RESTIC_REPOSITORY"] == "sftp:host:/myrepo"


def test_build_backend_env_sets_password_when_present() -> None:
    from k8si.cli import _build_backend_env

    cfg = _make_config(restic_password="mysecret", restic_password_file=None)
    env = _build_backend_env(cfg)
    assert env["RESTIC_PASSWORD"] == "mysecret"
    assert "RESTIC_PASSWORD_FILE" not in env


def test_build_backend_env_sets_password_file_when_no_password() -> None:
    from pathlib import Path

    from k8si.cli import _build_backend_env

    cfg = _make_config(restic_password=None, restic_password_file=Path("/run/secrets/pw"))
    env = _build_backend_env(cfg)
    assert env["RESTIC_PASSWORD_FILE"] == "/run/secrets/pw"
    assert "RESTIC_PASSWORD" not in env


def test_build_backend_env_neither_password_nor_file() -> None:
    from k8si.cli import _build_backend_env

    cfg = _make_config(restic_password=None, restic_password_file=None)
    env = _build_backend_env(cfg)
    assert "RESTIC_PASSWORD" not in env
    assert "RESTIC_PASSWORD_FILE" not in env


# ---------------------------------------------------------------------------
# main() — generate subcommand branch
# ---------------------------------------------------------------------------


def test_main_generate_subcommand_dispatches_to_generate(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() delegates to generate.run when 'generate' is first argv."""
    import io

    monkeypatch.setattr(
        "sys.argv",
        [
            "k8si",
            "generate",
            "--app=a",
            "--pvc=b",
            "--secret=s",
            "--sentinel=x.xml",
            "--schedule=0 2 * * *",
        ],
    )
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)

    from k8si import cli

    cli.main()  # must return normally, not sys.exit()
    assert "k8si-restore" in buf.getvalue()


# ---------------------------------------------------------------------------
# main() — runtime branch (CONFIG → backend → mode dispatch)
# ---------------------------------------------------------------------------


def test_main_config_error_exits_with_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() logs error and exits(1) when Config.from_env raises ConfigError."""
    from k8si.config import ConfigError

    monkeypatch.setattr("sys.argv", ["k8si"])
    with patch("k8si.cli.Config.from_env", side_effect=ConfigError("bad config")):
        with pytest.raises(SystemExit) as exc_info:
            from k8si import cli

            cli.main()
    assert exc_info.value.code == 1


def test_main_restore_mode_calls_restore_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() with mode='restore' calls restore.run(config, backend)."""
    monkeypatch.setattr("sys.argv", ["k8si"])
    cfg = _make_config(mode="restore", backend_type="restic")

    with (
        patch("k8si.cli.Config.from_env", return_value=cfg),
        patch("k8si.cli.ResticBackend"),
        patch("k8si.restore.run") as mock_restore,
    ):
        from k8si import cli

        cli.main()

    mock_restore.assert_called_once()


def test_main_job_mode_calls_backup_run_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() with mode='job' calls backup.run_once(config, backend)."""
    monkeypatch.setattr("sys.argv", ["k8si"])
    cfg = _make_config(mode="job", backend_type="restic")

    with (
        patch("k8si.cli.Config.from_env", return_value=cfg),
        patch("k8si.cli.ResticBackend"),
        patch("k8si.backup.run_once") as mock_run_once,
    ):
        from k8si import cli

        cli.main()

    mock_run_once.assert_called_once()


def test_main_backup_mode_calls_backup_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() with mode='backup' calls backup.run(config, backend)."""
    monkeypatch.setattr("sys.argv", ["k8si"])
    cfg = _make_config(mode="backup", backend_type="restic")

    with (
        patch("k8si.cli.Config.from_env", return_value=cfg),
        patch("k8si.cli.ResticBackend"),
        patch("k8si.backup.run") as mock_run,
    ):
        from k8si import cli

        cli.main()

    mock_run.assert_called_once()


def test_main_backup_mode_crash_logs_critical_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """main() with mode='backup': if backup.run() raises, main() logs one clear
    CRITICAL line naming the mode before exiting 1 — instead of letting the
    exception escape uncaught as a raw traceback with no top-level context."""
    monkeypatch.setattr("sys.argv", ["k8si"])
    cfg = _make_config(mode="backup", backend_type="restic")

    with (
        patch("k8si.cli.Config.from_env", return_value=cfg),
        patch("k8si.cli.ResticBackend"),
        patch("k8si.backup.run", side_effect=RuntimeError("repo unreachable")),
        caplog.at_level("CRITICAL"),
    ):
        from k8si import cli

        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    assert exc_info.value.code == 1
    critical_records = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert len(critical_records) == 1
    assert "backup" in critical_records[0].message
    assert critical_records[0].exc_info is not None


def test_main_kopia_backend_instantiates_kopia(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() with backend_type='kopia' creates a KopiaBackend."""
    monkeypatch.setattr("sys.argv", ["k8si"])
    cfg = _make_config(mode="restore", backend_type="kopia")

    with (
        patch("k8si.cli.Config.from_env", return_value=cfg),
        patch("k8si.cli.KopiaBackend") as mock_kopia,
        patch("k8si.restore.run"),
    ):
        from k8si import cli

        cli.main()

    mock_kopia.assert_called_once()
