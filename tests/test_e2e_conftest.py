"""Unit tests for e2e/conftest.py's storageclass auto-detection logic."""

from types import SimpleNamespace

from e2e.conftest import (
    _mariadb_healthcheck_command,
    _mariadb_readiness_probe,
    _pick_storage_class,
)


def _sc(name, annotations=None):
    return SimpleNamespace(metadata=SimpleNamespace(name=name, annotations=annotations))


def test_pick_storage_class_prefers_default():
    classes = [
        _sc("linstor-worker-local"),
        _sc(
            "linstor-stor01-local",
            annotations={"storageclass.kubernetes.io/is-default-class": "true"},
        ),
    ]
    assert _pick_storage_class(classes) == "linstor-stor01-local"


def test_pick_storage_class_skips_smb_and_replicated():
    classes = [
        _sc("smb-csi"),
        _sc("linstor-worker-replicated"),
        _sc("linstor-worker-local"),
    ]
    assert _pick_storage_class(classes) == "linstor-worker-local"


def test_pick_storage_class_falls_back_to_replicated_if_no_other_option():
    classes = [_sc("smb-csi"), _sc("linstor-worker-replicated")]
    assert _pick_storage_class(classes) == "linstor-worker-replicated"


def test_pick_storage_class_empty_returns_none():
    assert _pick_storage_class([]) is None


def test_mariadb_healthcheck_command_puts_su_mysql_first():
    # healthcheck.sh docs: --su-mysql re-execs the script as the mysql unix
    # user and "disregards previous options set, so should usually be the
    # first option" -- if it isn't first, --connect runs as the wrong user
    # and fails before --su-mysql ever takes effect.
    command = _mariadb_healthcheck_command()
    assert command[0] == "healthcheck.sh"
    assert command[1] == "--su-mysql"
    assert set(command[2:]) == {"--connect", "--innodb_initialized"}


def test_mariadb_readiness_probe_timeout_covers_slow_exec():
    # Run 1385/1381: healthcheck.sh consistently timed out at the old 10s
    # limit under CPU-limited runner load, every single attempt, for the
    # entire 300s wait -- readiness was never reached. Give the exec probe
    # real headroom.
    probe = _mariadb_readiness_probe()
    assert probe["timeoutSeconds"] >= 30


def test_mariadb_readiness_probe_period_does_not_overlap_timeout():
    # periodSeconds < timeoutSeconds lets kubelet queue up overlapping probe
    # execs, which only adds more contention on an already-slow exec.
    probe = _mariadb_readiness_probe()
    assert probe["periodSeconds"] >= probe["timeoutSeconds"]
