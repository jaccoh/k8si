"""Operator wiring of the restore patch's provenance env.

build_restore_patch(spec, name=..., namespace=...) emits
K8SI_BACKUP_NAME/K8SI_BACKUP_NAMESPACE so the restore init container can
report back onto the K8siBackup CR (see tests/test_restore_patch_provenance.py
for the patch-side contract). These tests pin the two operator call sites —
the CR's own name/namespace must reach the patch, otherwise restore reporting
silently stays off for every backup in the cluster.
"""

import logging
from unittest.mock import patch

import yaml

from tests.helpers import BODY, SPEC, FakePatch


def _restore_container_env(raw_patch: str) -> dict[str, str]:
    clean = "\n".join(line for line in raw_patch.splitlines() if not line.strip().startswith("#"))
    items = yaml.safe_load(clean)
    container = next(i for i in items if isinstance(i, dict) and i.get("name") == "k8si-restore")
    return {e["name"]: e.get("value", "__secretRef__") for e in container["env"]}


def test_on_create_wires_provenance_into_restore_patch() -> None:
    from k8si.operator.main import on_create

    patch_obj = FakePatch()
    with patch("k8si.operator.main.kopf.event"):
        on_create(
            body=BODY,
            spec={**SPEC, "pvc": "sonarr-config"},
            name="sonarr-config",
            namespace="downloads",
            patch=patch_obj,  # type: ignore[arg-type]
            logger=logging.getLogger("test"),
        )

    env = _restore_container_env(patch_obj.status["restorePatch"])
    assert env.get("K8SI_BACKUP_NAME") == "sonarr-config"
    assert env.get("K8SI_BACKUP_NAMESPACE") == "downloads"


def test_on_update_wires_provenance_into_restore_patch() -> None:
    from k8si.operator.main import on_update

    patch_obj = FakePatch()
    with patch("k8si.operator.main.kopf.event"):
        on_update(
            body=BODY,
            spec=SPEC,
            name="media",
            namespace="media-ns",
            patch=patch_obj,  # type: ignore[arg-type]
            logger=logging.getLogger("test"),
        )

    env = _restore_container_env(patch_obj.status["restorePatch"])
    assert env.get("K8SI_BACKUP_NAME") == "media"
    assert env.get("K8SI_BACKUP_NAMESPACE") == "media-ns"
