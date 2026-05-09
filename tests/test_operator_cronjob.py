"""Tests for k8si/operator/cronjob.py — restore patch generator."""

import yaml

from k8si.operator.cronjob import build_restore_patch

_BASE_SPEC: dict = {
    "pvc": "myapp-data",
    "resticSecret": "restic-myapp",
    "schedule": "0 2 * * *",
}


def _patch_env(spec: dict) -> dict[str, str]:
    """Parse the YAML restore patch and return the container env name→value dict."""
    raw = build_restore_patch(spec)
    clean = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("#"))
    items = yaml.safe_load(clean)
    container = items[0]
    result = {}
    for e in container["env"]:
        result[e["name"]] = e.get("value", "__secretRef__")
    return result


class TestBuildRestorePatchTags:
    def test_restore_tags_inherit_from_spec_tags_when_not_explicit(self):
        spec = {**_BASE_SPEC, "tags": ["app=myapp"]}
        env = _patch_env(spec)
        assert env["RESTORE_TAGS"] == "app=myapp"

    def test_explicit_restore_tags_override_spec_tags(self):
        spec = {**_BASE_SPEC, "tags": ["app=myapp"], "restore": {"tags": ["custom=true"]}}
        env = _patch_env(spec)
        assert env["RESTORE_TAGS"] == "custom=true"

    def test_no_restore_tags_when_neither_set(self):
        env = _patch_env(_BASE_SPEC)
        assert "RESTORE_TAGS" not in env

    def test_restore_tags_empty_list_produces_no_env(self):
        spec = {**_BASE_SPEC, "tags": [], "restore": {"tags": []}}
        env = _patch_env(spec)
        assert "RESTORE_TAGS" not in env


class TestBuildRestorePatchStructure:
    def test_mount_path_is_always_slash_data(self):
        raw = build_restore_patch(_BASE_SPEC)
        assert "mountPath: /data" in raw

    def test_sentinels_in_env(self):
        spec = {**_BASE_SPEC, "restore": {"sentinels": ["config.xml", "db/version"]}}
        env = _patch_env(spec)
        assert env["RESTORE_SENTINELS"] == "config.xml,db/version"

    def test_no_sentinels_env_when_absent(self):
        env = _patch_env(_BASE_SPEC)
        assert "RESTORE_SENTINELS" not in env

    def test_required_true_sets_env(self):
        spec = {**_BASE_SPEC, "restore": {"required": True}}
        env = _patch_env(spec)
        assert env["RESTORE_REQUIRED"] == "true"

    def test_required_false_omits_env(self):
        env = _patch_env({**_BASE_SPEC, "restore": {"required": False}})
        assert "RESTORE_REQUIRED" not in env

    def test_max_age_in_env(self):
        spec = {**_BASE_SPEC, "restore": {"maxAge": "7d"}}
        env = _patch_env(spec)
        assert env["RESTORE_MAX_AGE"] == "7d"

    def test_size_min_in_env(self):
        spec = {**_BASE_SPEC, "restore": {"size": {"min": "1Mi"}}}
        env = _patch_env(spec)
        assert env["RESTORE_SIZE_MIN"] == "1Mi"

    def test_size_max_in_env(self):
        spec = {**_BASE_SPEC, "restore": {"size": {"max": "50Gi"}}}
        env = _patch_env(spec)
        assert env["RESTORE_SIZE_MAX"] == "50Gi"

    def test_restic_secret_ref_in_env(self):
        env = _patch_env(_BASE_SPEC)
        for var in ("RESTIC_REPOSITORY", "RESTIC_PASSWORD", "RESTIC_SFTP_COMMAND"):
            assert env[var] == "__secretRef__"

    def test_restic_secret_name_in_patch(self):
        raw = build_restore_patch(_BASE_SPEC)
        assert "restic-myapp" in raw

    def test_mode_is_restore(self):
        env = _patch_env(_BASE_SPEC)
        assert env["MODE"] == "restore"
