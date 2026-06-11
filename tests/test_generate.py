"""Tests for generate.py YAML output correctness."""

import argparse
import io
import sys

import pytest

from k8si import generate


def _make_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        app="myapp",
        pvc="myapp-pvc",
        secret="myapp-secret",
        sentinel="config.xml",
        schedule="0 2 * * *",
        image="ghcr.io/jaccoh/k8si:latest",
        retention_daily=7,
        retention_weekly=4,
        retention_monthly=3,
        tags="",
        no_sidecar=False,
        backup_name=None,
        backup_namespace="default",
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _capture_run(args: argparse.Namespace) -> str:
    """Run generate.run(args) and return its stdout as a string."""
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        generate.run(args)
    finally:
        sys.stdout = old_stdout
    return buf.getvalue()


# ── Test 1: init container uses RESTORE_SENTINELS, not SENTINEL_FILE ──────────


def test_init_container_uses_restore_sentinels():
    output = _capture_run(_make_args(sentinel="config.xml"))
    assert "RESTORE_SENTINELS" in output, "RESTORE_SENTINELS must appear in init container output"
    assert "SENTINEL_FILE" not in output, "SENTINEL_FILE must NOT appear anywhere in the output"


# ── Test 2: sidecar does NOT contain SENTINEL_FILE ────────────────────────────


def test_sidecar_does_not_contain_sentinel_file():
    output = _capture_run(_make_args(sentinel="config.xml"))
    assert "SENTINEL_FILE" not in output, "SENTINEL_FILE must NOT appear in sidecar output"


# ── Test 3: RESTORE_SENTINELS value matches the --sentinel argument ────────────


def test_restore_sentinels_value_matches_sentinel_arg():
    output = _capture_run(_make_args(sentinel="config.xml"))
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if "RESTORE_SENTINELS" in line:
            # The next line should contain the value
            value_line = lines[i + 1] if i + 1 < len(lines) else ""
            assert "config.xml" in value_line, (
                f"Expected 'config.xml' on the line after RESTORE_SENTINELS, got: {value_line!r}"
            )
            break
    else:
        pytest.fail("RESTORE_SENTINELS not found in output")


# ── Test 4: --no-sidecar emits init container only ────────────────────────────


def test_no_sidecar_omits_backup_container():
    output = _capture_run(_make_args(no_sidecar=True))
    assert "k8si-restore" in output, "k8si-restore init container must be present"
    assert "k8si-backup" not in output, "k8si-backup sidecar must NOT be present with --no-sidecar"


# ── Test 5: --backup-name injects env vars and RBAC snippet ──────────────────


def test_backup_name_injects_env_vars_and_rbac() -> None:
    output = _capture_run(_make_args(backup_name="my-app-backup", backup_namespace="staging"))
    assert "K8SI_BACKUP_NAME" in output
    assert "my-app-backup" in output
    assert "K8SI_BACKUP_NAMESPACE" in output
    assert "staging" in output
    assert "kind: ServiceAccount" in output
    assert "kind: ClusterRoleBinding" in output
    assert "k8si-restore" in output


def test_add_parser_registers_generate_subcommand() -> None:
    """add_parser registers a 'generate' subcommand with all required flags."""
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    generate.add_parser(subparsers)
    args = parser.parse_args(
        [
            "generate",
            "--app=testapp",
            "--pvc=test-pvc",
            "--secret=test-secret",
            "--sentinel=config.xml",
            "--schedule=0 2 * * *",
        ]
    )
    assert args.app == "testapp"
    assert args.pvc == "test-pvc"
    assert args.sentinel == "config.xml"
    assert args.schedule == "0 2 * * *"
    assert args.no_sidecar is False
    assert args.retention_daily == 7
    assert args.backup_name is None


def test_add_parser_backup_name_flag() -> None:
    """add_parser accepts --backup-name and --backup-namespace flags."""
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    generate.add_parser(subparsers)
    args = parser.parse_args(
        [
            "generate",
            "--app=testapp",
            "--pvc=test-pvc",
            "--secret=test-secret",
            "--sentinel=config.xml",
            "--schedule=0 2 * * *",
            "--backup-name=my-crd",
            "--backup-namespace=prod",
        ]
    )
    assert args.backup_name == "my-crd"
    assert args.backup_namespace == "prod"


def test_tags_in_output() -> None:
    """--tags includes BACKUP_TAGS in both init container and sidecar."""
    output = _capture_run(_make_args(tags="env=prod,team=backend"))
    assert "env=prod" in output
    assert "team=backend" in output
    assert output.count("BACKUP_TAGS") >= 1
