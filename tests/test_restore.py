"""Tests for restore (init container) mode."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from k8si.backend import BackupError
from k8si.config import Config
from k8si.restore import MARKER_FILE, NO_RESTORE_FILE, run


def make_config(
    tmp_path: Path,
    sentinels: list[str] | None = None,
    required: bool = False,
    restore_tags: list[str] | None = None,
    max_age_hours: float | None = None,
    size_min: int | None = None,
    size_max: int | None = None,
    restore_snapshot: str | None = None,
    backup_name: str | None = None,
    backup_namespace: str | None = None,
) -> Config:
    return Config(
        mode="restore",
        data_path=tmp_path,
        restic_repository="sftp:fake",
        restic_password="secret",
        restic_password_file=None,
        restore_sentinels=sentinels if sentinels is not None else ["config.xml"],
        restore_required=required,
        restore_tags=restore_tags or [],
        restore_max_age_hours=max_age_hours,
        restore_size_min=size_min,
        restore_size_max=size_max,
        restore_snapshot=restore_snapshot,
        backup_name=backup_name,
        backup_namespace=backup_namespace,
    )


def _backend_with_snapshot(
    snapshot_id: str = "abc12345", sentinels_present: bool = True
) -> MagicMock:
    backend = MagicMock()
    backend.snapshots.return_value = [
        {
            "id": snapshot_id,
            "short_id": snapshot_id[:8],
            "time": "2026-05-07T19:00:00Z",
            "tags": [],
        }
    ]
    backend.check_sentinels.return_value = sentinels_present
    backend.snapshot_size.return_value = 10 * 1024 * 1024  # 10 MiB
    return backend


# ── skip conditions ────────────────────────────────────────────────────────────


def test_skips_when_all_sentinels_present(tmp_path: Path) -> None:
    (tmp_path / "config.xml").touch()
    backend = MagicMock()
    run(make_config(tmp_path), backend)
    backend.restore.assert_not_called()


def test_skips_when_no_restore_file_present(tmp_path: Path) -> None:
    (tmp_path / NO_RESTORE_FILE).touch()
    backend = MagicMock()
    run(make_config(tmp_path), backend)
    backend.restore.assert_not_called()


def test_skips_when_marker_present_no_sentinels(tmp_path: Path) -> None:
    (tmp_path / MARKER_FILE).write_text("restored\n")
    backend = MagicMock()
    run(make_config(tmp_path, sentinels=[]), backend)
    backend.restore.assert_not_called()


def test_skips_when_no_snapshots_not_required(tmp_path: Path) -> None:
    backend = MagicMock()
    backend.snapshots.return_value = []
    run(make_config(tmp_path), backend)
    backend.restore.assert_not_called()


# ── fail-loud conditions ───────────────────────────────────────────────────────


def test_fails_when_marker_present_but_sentinels_missing(tmp_path: Path) -> None:
    (tmp_path / MARKER_FILE).write_text("restored\n")
    backend = MagicMock()
    with pytest.raises(SystemExit):
        run(make_config(tmp_path), backend)


def test_fails_when_no_snapshots_and_required(tmp_path: Path) -> None:
    backend = MagicMock()
    backend.snapshots.return_value = []
    with pytest.raises(SystemExit):
        run(make_config(tmp_path, required=True), backend)


def test_fails_when_snapshots_raises_transport_error(tmp_path: Path) -> None:
    from k8si.backend import BackupError

    backend = MagicMock()
    backend.snapshots.side_effect = BackupError(
        "connection refused", 1, "ssh: connect: Connection refused"
    )
    with pytest.raises(SystemExit):
        run(make_config(tmp_path), backend)


def test_fails_when_backend_restore_errors(tmp_path: Path) -> None:
    backend = _backend_with_snapshot()
    backend.restore.side_effect = BackupError("failed", 1, "connection refused")
    with pytest.raises(SystemExit):
        run(make_config(tmp_path), backend)


# ── snapshot quality gates ─────────────────────────────────────────────────────


def test_skips_when_sentinel_missing_from_snapshot(tmp_path: Path) -> None:
    backend = _backend_with_snapshot(sentinels_present=False)
    run(make_config(tmp_path), backend)
    backend.restore.assert_not_called()


def test_skips_when_snapshot_too_small(tmp_path: Path) -> None:
    backend = _backend_with_snapshot()
    backend.snapshot_size.return_value = 100  # 100 bytes
    run(make_config(tmp_path, size_min=1024 * 1024), backend)
    backend.restore.assert_not_called()


def test_skips_when_snapshot_too_large(tmp_path: Path) -> None:
    backend = _backend_with_snapshot()
    backend.snapshot_size.return_value = 100 * 1024 * 1024  # 100 MiB
    run(make_config(tmp_path, size_max=50 * 1024 * 1024), backend)
    backend.restore.assert_not_called()


# ── successful restore ─────────────────────────────────────────────────────────


def test_restores_and_writes_marker(tmp_path: Path) -> None:
    backend = _backend_with_snapshot()
    backend.restore.side_effect = lambda **_: (tmp_path / "config.xml").touch()
    run(make_config(tmp_path), backend)
    backend.restore.assert_called_once()
    assert (tmp_path / MARKER_FILE).exists()


def test_restore_uses_pinned_snapshot(tmp_path: Path) -> None:
    backend = _backend_with_snapshot()
    backend.restore.side_effect = lambda **_: (tmp_path / "config.xml").touch()
    run(make_config(tmp_path, restore_snapshot="deadbeef"), backend)
    backend.restore.assert_called_once_with(snapshot_id="deadbeef")
    backend.snapshots.assert_not_called()


def test_restore_passes_tags_to_snapshots(tmp_path: Path) -> None:
    backend = _backend_with_snapshot()
    backend.restore.side_effect = lambda **_: (tmp_path / "config.xml").touch()
    run(make_config(tmp_path, restore_tags=["app=prowlarr"]), backend)
    backend.snapshots.assert_called_once_with(tags=["app=prowlarr"])


def test_fails_when_sentinels_missing_after_restore(tmp_path: Path) -> None:
    backend = _backend_with_snapshot()
    # restore completes but sentinel is never written
    with pytest.raises(SystemExit):
        run(make_config(tmp_path), backend)


# ── restore reporting ──────────────────────────────────────────────────────────


def test_success_reports_to_crd(tmp_path: Path) -> None:
    backend = _backend_with_snapshot()
    backend.restore.side_effect = lambda **_: (tmp_path / "config.xml").touch()
    cfg = make_config(tmp_path, backup_name="my-backup", backup_namespace="default")
    with (
        patch("k8si.restore.kubernetes.config.load_incluster_config"),
        patch("k8si.restore.kubernetes.client.CustomObjectsApi") as mock_cls,
    ):
        run(cfg, backend)
    body = mock_cls.return_value.patch_namespaced_custom_object_status.call_args.kwargs["body"]
    assert body["status"]["lastRestoreResult"] == "success"
    assert body["status"]["lastRestoreSnapshotId"] == "abc12345"


def test_failure_still_reports_to_crd(tmp_path: Path) -> None:
    backend = _backend_with_snapshot()
    backend.restore.side_effect = BackupError("failed", 1, "connection refused")
    cfg = make_config(tmp_path, backup_name="my-backup", backup_namespace="default")
    with (
        patch("k8si.restore.kubernetes.config.load_incluster_config"),
        patch("k8si.restore.kubernetes.client.CustomObjectsApi") as mock_cls,
    ):
        with pytest.raises(SystemExit):
            run(cfg, backend)
    body = mock_cls.return_value.patch_namespaced_custom_object_status.call_args.kwargs["body"]
    assert body["status"]["lastRestoreResult"] == "failed"


def test_skipped_reports_to_crd(tmp_path: Path) -> None:
    (tmp_path / "config.xml").touch()
    backend = MagicMock()
    cfg = make_config(tmp_path, backup_name="my-backup", backup_namespace="default")
    with (
        patch("k8si.restore.kubernetes.config.load_incluster_config"),
        patch("k8si.restore.kubernetes.client.CustomObjectsApi") as mock_cls,
    ):
        run(cfg, backend)
    body = mock_cls.return_value.patch_namespaced_custom_object_status.call_args.kwargs["body"]
    assert body["status"]["lastRestoreResult"] == "skipped"


# ── _pod_namespace: reads from SA file (lines 21-24) ──────────────────────────


def test_pod_namespace_reads_from_sa_file(tmp_path: Path) -> None:
    """_pod_namespace() reads the namespace from the in-cluster service account file."""
    ns_file = tmp_path / "namespace"
    ns_file.write_text("prod\n")

    with patch("k8si.restore.Path") as mock_path_cls:
        mock_path_cls.return_value = ns_file
        from k8si.restore import _pod_namespace

        result = _pod_namespace()

    assert result == "prod"


def test_pod_namespace_defaults_to_default_when_no_file() -> None:
    """_pod_namespace() returns 'default' when the SA namespace file is absent."""
    with patch("k8si.restore.Path") as mock_path_cls:
        mock_path_cls.return_value.exists.return_value = False
        from k8si.restore import _pod_namespace

        result = _pod_namespace()

    assert result == "default"


# ── _report_to_crd: ConfigException fallback to kube_config (lines 42-43) ────


def test_report_to_crd_falls_back_to_kube_config(tmp_path: Path) -> None:
    """_report_to_crd() falls back to load_kube_config when incluster fails."""
    import kubernetes.config

    cfg = make_config(tmp_path, backup_name="my-backup", backup_namespace="ns")
    result = {"result": "success", "snapshot_id": "abc", "message": "ok"}

    with (
        patch(
            "k8si.restore.kubernetes.config.load_incluster_config",
            side_effect=kubernetes.config.ConfigException("not in cluster"),
        ),
        patch("k8si.restore.kubernetes.config.load_kube_config") as mock_kube,
        patch("k8si.restore.kubernetes.client.CustomObjectsApi") as mock_cls,
    ):
        from k8si.restore import _report_to_crd

        _report_to_crd(cfg, result)

    mock_kube.assert_called_once()
    mock_cls.return_value.patch_namespaced_custom_object_status.assert_called_once()


# ── pinned snapshot missing sentinels → SystemExit (lines 120-121) ────────────


def test_pinned_snapshot_missing_sentinels_raises(tmp_path: Path) -> None:
    """A pinned snapshot that fails sentinel check causes SystemExit(1)."""
    backend = MagicMock()
    backend.check_sentinels.return_value = False
    cfg = make_config(tmp_path, restore_snapshot="deadbeef", sentinels=["config.xml"])
    with pytest.raises(SystemExit):
        run(cfg, backend)


# ── age check: too old snapshot returns None (lines 158-168) ──────────────────


def test_snapshot_too_old_is_skipped(tmp_path: Path) -> None:
    """A snapshot older than restore_max_age_hours is skipped (not an error)."""
    backend = MagicMock()
    backend.snapshots.return_value = [
        {"id": "abc12345", "short_id": "abc12345", "time": "2020-01-01T02:00:00Z"}
    ]
    cfg = make_config(tmp_path, max_age_hours=1.0, sentinels=[])
    run(cfg, backend)  # must not raise
    backend.restore.assert_not_called()


def test_recent_snapshot_within_age_limit_proceeds(tmp_path: Path) -> None:
    """A recent snapshot within the age limit logs info and proceeds to restore."""
    from datetime import UTC, datetime

    recent_time = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    backend = MagicMock()
    backend.snapshots.return_value = [
        {"id": "abc12345", "short_id": "abc12345", "time": recent_time}
    ]
    backend.restore.side_effect = lambda **_: (tmp_path / "config.xml").touch()
    cfg = make_config(tmp_path, max_age_hours=24.0)
    run(cfg, backend)
    backend.restore.assert_called_once()


# ── size check: within bounds logs info (line 194) ────────────────────────────


def test_snapshot_within_size_bounds_proceeds(tmp_path: Path) -> None:
    """A snapshot within size bounds logs info and proceeds to restore."""
    backend = _backend_with_snapshot()
    backend.snapshot_size.return_value = 50 * 1024 * 1024  # 50 MiB
    backend.restore.side_effect = lambda **_: (tmp_path / "config.xml").touch()
    cfg = make_config(
        tmp_path,
        size_min=10 * 1024 * 1024,  # 10 MiB
        size_max=100 * 1024 * 1024,  # 100 MiB
    )
    run(cfg, backend)
    backend.restore.assert_called_once()


# ── _sentinels_in_snapshot: empty sentinel list → True (line 206) ─────────────


def test_sentinels_in_snapshot_empty_list_returns_true() -> None:
    """_sentinels_in_snapshot() returns True immediately when sentinels is empty."""
    from k8si.restore import _sentinels_in_snapshot

    backend = MagicMock()
    result = _sentinels_in_snapshot(backend, "abc1234", [])
    assert result is True
    backend.check_sentinels.assert_not_called()


# ── _do_restore: lock contention → return False (lines 225-228) ───────────────


def test_do_restore_skips_when_lock_held(tmp_path: Path) -> None:
    """_do_restore() returns False when another process holds the file lock."""

    from k8si.restore import _do_restore

    backend = MagicMock()
    with patch("fcntl.flock", side_effect=BlockingIOError):
        result = _do_restore(
            backend,
            snapshot_id="abc12345",
            data_path=tmp_path,
            sentinels=[],
            marker=tmp_path / ".k8si-restore-complete",
        )
    assert result is False
    backend.restore.assert_not_called()
