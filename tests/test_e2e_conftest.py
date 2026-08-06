"""Unit tests for e2e/conftest.py's storageclass auto-detection logic."""

from types import SimpleNamespace

from e2e.conftest import _pick_storage_class


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
