"""Unit tests for e2e/conftest.py's storageclass auto-detection logic."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import kubernetes.config

from e2e.conftest import (
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


def test_mariadb_readiness_probe_uses_tcp_socket():
    # Run 1444: an exec healthcheck.sh probe left orphaned processes behind
    # on every kubelet timeout (containerd doesn't kill the in-container
    # process tree), and orphans piled up until they saturated the container's
    # cpu limit -- no exec, no zombies.
    probe = _mariadb_readiness_probe()
    assert probe["tcpSocket"] == {"port": 3306}
    assert "exec" not in probe


def test_detect_environment_survives_missing_cluster(monkeypatch):
    """_detect_environment() runs at e2e.conftest IMPORT time, so it executes
    on every `pytest tests/` run -- including on GitHub-hosted runners with no
    kubeconfig at all. It must degrade to placeholders, not raise, or the whole
    unit suite fails at collection (GitHub CI `test` has been red on this)."""
    import e2e.conftest as conftest

    monkeypatch.delenv("E2E_NODE_NAME", raising=False)
    monkeypatch.delenv("E2E_STORAGE_CLASS", raising=False)
    monkeypatch.delenv("E2E_SNAPSHOT_CLASS", raising=False)
    monkeypatch.setattr(
        kubernetes.config,
        "load_incluster_config",
        MagicMock(side_effect=kubernetes.config.ConfigException("no in-cluster")),
    )
    monkeypatch.setattr(
        kubernetes.config,
        "load_kube_config",
        MagicMock(side_effect=kubernetes.config.ConfigException("Invalid kube-config file")),
    )

    node, sc, vsc = conftest._detect_environment()
    assert node == "unknown"
    assert sc == "unknown"
    assert vsc == "unknown"
