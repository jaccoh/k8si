"""Restore-patch provenance env (K8SI_BACKUP_NAME / K8SI_BACKUP_NAMESPACE / backend).

`build_restore_patch` emits the init container users paste into their own pod
spec. For the restore container to report status back onto the K8siBackup CR
(`lastRestoreResult` etc., see `k8si/restore.py`) it must know which CR it
belongs to. These tests pin that wiring.
"""

import os

import yaml

from k8si.operator.cronjob import build_restore_patch

_BASE_SPEC: dict = {
    "pvc": "myapp-data",
    "resticSecret": "restic-myapp",
    "schedule": "0 2 * * *",
}


def _patch_env(spec: dict, **kwargs: object) -> dict[str, str]:
    """Build the patch and return the k8si-restore container env as name→value."""
    raw = build_restore_patch(spec, **kwargs)
    clean = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("#"))
    items = yaml.safe_load(clean)
    container = next(i for i in items if isinstance(i, dict) and i.get("name") == "k8si-restore")
    return {e["name"]: e.get("value", "__secretRef__") for e in container["env"]}


class TestProvenanceEnv:
    def test_backup_name_and_namespace_included_when_provided(self):
        env = _patch_env(_BASE_SPEC, name="sonarr-config", namespace="downloads")
        assert env["K8SI_BACKUP_NAME"] == "sonarr-config"
        assert env["K8SI_BACKUP_NAMESPACE"] == "downloads"

    def test_provenance_env_omitted_when_name_missing(self):
        """No CR name → no reporting, matching k8si/restore.py's opt-in contract."""
        env = _patch_env(_BASE_SPEC)
        assert "K8SI_BACKUP_NAME" not in env
        assert "K8SI_BACKUP_NAMESPACE" not in env

    def test_empty_name_omits_both(self):
        env = _patch_env(_BASE_SPEC, name="", namespace="downloads")
        assert "K8SI_BACKUP_NAME" not in env
        assert "K8SI_BACKUP_NAMESPACE" not in env

    def test_namespace_defaults_to_default_when_only_name_given(self):
        """A name without a namespace still reports — the CR is looked up in `default`."""
        env = _patch_env(_BASE_SPEC, name="my-backup")
        assert env["K8SI_BACKUP_NAME"] == "my-backup"
        assert env["K8SI_BACKUP_NAMESPACE"] == "default"

    def test_namespace_alone_emits_neither(self):
        """A namespace without a name is not enough to report, so nothing is emitted."""
        env = _patch_env(_BASE_SPEC, namespace="downloads")
        assert "K8SI_BACKUP_NAME" not in env
        assert "K8SI_BACKUP_NAMESPACE" not in env

    def test_provenance_env_survives_secret_ref_envs(self):
        """Provenance vars must not displace the credential secretKeyRef entries."""
        env = _patch_env(_BASE_SPEC, name="myapp", namespace="myapp-ns")
        for var in ("RESTIC_REPOSITORY", "RESTIC_PASSWORD", "RESTIC_SFTP_COMMAND"):
            assert env[var] == "__secretRef__"

    def test_provenance_env_does_not_break_the_patch_yaml(self):
        """The whole patch must still round-trip through a YAML parser."""
        raw = build_restore_patch(_BASE_SPEC, name="myapp", namespace="myapp-ns")
        clean = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("#"))
        items = yaml.safe_load(clean)
        assert isinstance(items, list) and len(items) == 4


class TestBackendTypeEnv:
    def test_backend_type_defaults_to_restic(self):
        os.environ.pop("BACKEND_TYPE", None)
        env = _patch_env(_BASE_SPEC, name="myapp")
        assert env["BACKEND_TYPE"] == "restic"

    def test_backend_type_from_env_is_forwarded(self, monkeypatch):
        monkeypatch.setenv("BACKEND_TYPE", "Kopia ")
        env = _patch_env(_BASE_SPEC, name="myapp")
        assert env["BACKEND_TYPE"] == "kopia"

    def test_spec_backend_type_overrides_operator_env(self, monkeypatch):
        """A per-CR backendType wins over the operator-wide BACKEND_TYPE."""
        monkeypatch.setenv("BACKEND_TYPE", "restic")
        env = _patch_env({**_BASE_SPEC, "backendType": "kopia"}, name="myapp")
        assert env["BACKEND_TYPE"] == "kopia"
