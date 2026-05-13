"""Tests for k8si/operator/cronjob.py — restore patch generator."""

import yaml

from k8si.operator.cronjob import build_restore_patch

_BASE_SPEC: dict = {
    "pvc": "myapp-data",
    "resticSecret": "restic-myapp",
    "schedule": "0 2 * * *",
}


def _parse_patch(spec: dict) -> list[dict]:
    """Parse the YAML restore patch into a list of items."""
    raw = build_restore_patch(spec)
    clean = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("#"))
    return yaml.safe_load(clean)


def _patch_env(spec: dict) -> dict[str, str]:
    """Parse the YAML restore patch and return the k8si-restore container env as name→value dict."""
    items = _parse_patch(spec)
    container = next(i for i in items if isinstance(i, dict) and i.get("name") == "k8si-restore")
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


class TestBuildRestorePatchFixSshPerms:
    def test_fix_ssh_perms_container_is_first(self):
        items = _parse_patch(_BASE_SPEC)
        assert items[0]["name"] == "fix-ssh-perms"

    def test_k8si_restore_is_second(self):
        items = _parse_patch(_BASE_SPEC)
        assert items[1]["name"] == "k8si-restore"

    def test_k8si_restore_has_security_context(self):
        items = _parse_patch(_BASE_SPEC)
        restore = items[1]
        assert restore["securityContext"]["runAsUser"] == 0
        assert restore["securityContext"]["runAsGroup"] == 0

    def test_fix_ssh_perms_runs_as_root(self):
        items = _parse_patch(_BASE_SPEC)
        assert items[0]["securityContext"]["runAsUser"] == 0

    def test_restic_ssh_emptydir_in_volumes(self):
        items = _parse_patch(_BASE_SPEC)
        volumes = [i for i in items if "emptyDir" in i or "secret" in i]
        names = [v["name"] for v in volumes]
        assert "restic-ssh" in names
        assert "restic-ssh-secret" in names

    def test_restic_ssh_secret_volume_renamed(self):
        items = _parse_patch(_BASE_SPEC)
        secret_vol = next(i for i in items if "secret" in i)
        assert secret_vol["name"] == "restic-ssh-secret"
        assert secret_vol["secret"]["secretName"] == "restic-myapp"


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
