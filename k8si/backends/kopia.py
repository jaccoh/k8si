"""Kopia backend — implements BackupBackend using the sh library.

Conforms to the BackupBackend protocol. Swappable with Restic.
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path

import sh

from ..backend import (
    BackupError,
    NoSnapshotsError,
    RepositoryLockedError,
    RepositoryNotInitializedError,
    SnapshotInfo,
)

log = logging.getLogger(__name__)


class KopiaBackend:
    """Wraps the kopia CLI via sh. All exceptions are converted to BackupError."""

    def __init__(self, env: dict[str, str]) -> None:
        self._config_file = env.get("KOPIA_CONFIG_PATH", "/tmp/kopia.config")
        self._repo = env.get("RESTIC_REPOSITORY", "")
        # kopia restores snapshot CONTENTS into the target dir (restic recreates
        # the absolute source path under --target /), so restore must aim at DATA_PATH
        self._data_path = env.get("DATA_PATH", "/data")
        # Pass password via env var (not CLI arg) to avoid leaking it in /proc/*/cmdline
        self._env = dict(env)
        self._env["KOPIA_PASSWORD"] = env.get("RESTIC_PASSWORD") or env.get("KOPIA_PASSWORD", "")
        self._k = sh.kopia.bake(
            "--config-file",
            self._config_file,
            _env=self._env,
            _encoding="utf-8",
            # kopia writes command results to stdout but progress and the
            # 'Created snapshot ... and ID ...' artifact line to stderr — merge
            # the streams so pod logs carry what _parse_artifact needs.
            _err_to_out=True,
        )
        self._connected = False
        self._last_source: str | None = None
        # Artifact captured from `snapshot create --json` (see backup()):
        # {"snapshotId": ..., "sizeBytes": ...} or None when unavailable.
        self.last_snapshot: dict | None = None

    def _ensure_connected(self) -> None:
        if self._connected:
            return

        # If config file exists and is non-empty, assume we are connected
        if os.path.exists(self._config_file) and os.path.getsize(self._config_file) > 0:
            self._connected = True
            return

        # Attempt to connect using sftp or filesystem based on self._repo
        args = ["repository", "connect"]
        if self._repo.startswith("sftp:"):
            sftp_args = self._parse_sftp_repo()
            args.extend(["sftp", *sftp_args])
        else:
            path = self._local_path()
            args.extend(["filesystem", f"--path={path}"])

        try:
            self._invoke(*args)
            self._connected = True
        except BackupError as e:
            if (
                "repository not initialized" in e.stderr.lower()
                or "cannot open" in e.stderr.lower()
            ):
                # Typed so the backup cycle auto-inits without string-matching
                # kopia's stderr (kopia never says "repository does not exist").
                raise RepositoryNotInitializedError(
                    "repository does not exist", e.returncode, e.stderr
                ) from e
            raise

    def _local_path(self) -> str:
        """Extract filesystem path from a local-backend repo URL."""
        if self._repo.startswith("local:"):
            return self._repo[len("local:") :]
        if self._repo.startswith("file://"):
            return self._repo[len("file://") :]
        return self._repo

    def _parse_sftp_repo(self) -> list[str]:
        # e.g. sftp:u12345@u12345.your-storagebox.de:backup/app
        match = re.match(r"^sftp:([^@]+)@([^:]+):(.+)$", self._repo)
        if not match:
            raise BackupError(f"Malformed SFTP repository URL: {self._repo}")
        username, host, path = match.groups()

        sftp_cmd = self._env.get("RESTIC_SFTP_COMMAND", "")
        port = "22"
        port_match = re.search(r"-p\s+(\d+)", sftp_cmd)
        if port_match:
            port = port_match.group(1)

        args = [
            f"--host={host}",
            f"--username={username}",
            f"--path={path}",
            f"--port={port}",
        ]

        if os.path.exists("/restic-ssh/id_ed25519"):
            args.append("--keyfile=/restic-ssh/id_ed25519")
        if os.path.exists("/restic-ssh/known_hosts"):
            args.append("--known-hosts=/restic-ssh/known_hosts")

        return args

    # ── BackupBackend protocol ─────────────────────────────────────────────────

    def init(self) -> None:
        args = ["repository", "create"]
        if self._repo.startswith("sftp:"):
            sftp_args = self._parse_sftp_repo()
            args.extend(["sftp", *sftp_args])
        else:
            path = self._local_path()
            args.extend(["filesystem", f"--path={path}"])

        self._invoke(*args)
        self._connected = True

    def snapshots(self, tags: list[str] | None = None) -> list[dict]:
        self._ensure_connected()
        raw = self._invoke("snapshot", "list", "--json")
        data = self._loads_list(raw)

        if tags:
            data = [s for s in data if self._has_tags(s, tags)]

        # Map Kopia format to match restic return format: [ {id, short_id, time} ]
        results = []
        for snap in data:
            results.append(
                {
                    "id": snap["id"],
                    # Kopia does not resolve restic-style 8-char ID prefixes, and
                    # restore.py restores via short_id — so it must be the full ID.
                    "short_id": snap["id"],
                    "time": snap["startTime"],
                }
            )
        return results

    @staticmethod
    def _has_tags(snap: dict, wanted: list[str]) -> bool:
        """Match restic-style 'key=value' tags against kopia's tag dict.

        kopia stores user tags under a 'tag:' prefix ({"tag:app": "x"}).
        """
        snap_tags = snap.get("tags", {})
        for tag in wanted:
            key, _, val = tag.partition("=")
            if snap_tags.get(key) != val and snap_tags.get(f"tag:{key}") != val:
                return False
        return True

    def ls(self, snapshot_id: str) -> list[str]:
        self._ensure_connected()
        raw = self._invoke("ls", "-r", snapshot_id)
        paths = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            # kopia ls -r formats output as indented or space-separated hierarchy,
            # or simply "path/to/file". Let's parse standard lines.
            parts = line.split()
            if parts:
                path = parts[-1]
                paths.append(path)
        return paths

    def check_sentinels(self, snapshot_id: str, sentinels: list[str]) -> bool:
        """Return True iff all sentinels exist in *snapshot_id*.

        Sentinel "data/foo" is matched against "/data/data/foo" (subPath layout),
        "/data/foo" (flat layout), "data/foo" (bare), and "<snapshot-id>/foo"
        (real kopia `ls -r` prefixes paths with the snapshot ID).
        """
        self._ensure_connected()
        if not sentinels:
            return True

        unfound = set(sentinels)

        # Stream kopia ls recursively (KOPIA_PASSWORD is in self._env)
        cmd = ["kopia", "--config-file", self._config_file, "ls", "-r", snapshot_id]
        with subprocess.Popen(
            cmd,
            env=self._env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        ) as proc:
            assert proc.stdout is not None
            for raw in proc.stdout:
                if not unfound:
                    proc.kill()
                    break
                raw = raw.strip()
                if not raw:
                    continue
                parts = raw.split()
                path = parts[-1]
                for sentinel in list(unfound):
                    if path == sentinel or path.endswith(f"/{sentinel}"):
                        unfound.discard(sentinel)
            _, stderr = proc.communicate()
            rc = proc.returncode
            if rc not in (0, -9) and unfound:
                raise BackupError(f"kopia ls exited {rc}", rc, stderr.strip())

        return not unfound

    def snapshot_size(self, snapshot_id: str) -> int:
        self._ensure_connected()
        # kopia 0.15.0 has no `snapshot show`; the size rides on
        # `snapshot list --json` as rootEntry.summ.size per manifest.
        raw = self._invoke("snapshot", "list", "--json")
        try:
            data = self._loads_list(raw)
            for snap in data:
                if snap.get("id") == snapshot_id:
                    return int(snap.get("rootEntry", {}).get("summ", {}).get("size", 0))
            return 0
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return 0

    def restore(self, snapshot_id: str = "latest") -> None:
        self._ensure_connected()
        try:
            self._invoke("snapshot", "restore", snapshot_id, self._data_path)
        except BackupError as e:
            if "not found" in e.stderr.lower():
                raise NoSnapshotsError("no snapshots in repository", e.returncode, e.stderr) from e
            raise

    def backup(self, source: Path, tags: list[str] | None = None) -> None:
        self._ensure_connected()
        self._last_source = str(source)  # store for forget()
        # Set tags if specified; kopia wants 'key:value', callers speak 'key=value'
        args = ["snapshot", "create", str(source)]
        if tags:
            for tag in tags:
                args.extend(["--tags", tag.replace("=", ":", 1)])
        # kopia 0.15.0 prints the snapshot manifest (with its ID) as JSON to
        # stdout — the structured artifact source the operator prefers over
        # scraping the human-readable 'Created snapshot ... and ID ...' line.
        args.append("--json")
        output = self._invoke(*args)
        self.last_snapshot = self._manifest_to_artifact(self._extract_manifest(output))

    @staticmethod
    def _extract_manifest(output: str) -> dict | None:
        """Find the snapshot manifest JSON in merged kopia output.

        Progress (stderr) is merged into stdout by _err_to_out and interleaves
        with the indented manifest, so scan every '{' and keep the last object
        that looks like a snapshot manifest.
        """
        decoder = json.JSONDecoder()
        best: dict | None = None
        idx = output.find("{")
        while idx != -1:
            try:
                obj, _end = decoder.raw_decode(output, idx)
            except json.JSONDecodeError:
                idx = output.find("{", idx + 1)
                continue
            if isinstance(obj, dict) and "id" in obj and ("startTime" in obj or "rootEntry" in obj):
                best = obj
            idx = output.find("{", idx + 1)
        return best

    @staticmethod
    def _manifest_to_artifact(manifest: dict | None) -> dict | None:
        if not manifest:
            return None
        size = manifest.get("rootEntry", {}).get("summ", {}).get("size")
        return {
            "snapshotId": str(manifest["id"]),
            "sizeBytes": int(size) if isinstance(size, (int, float)) else None,
        }

    def forget(self, daily: int, weekly: int, monthly: int, prune: bool = True) -> None:
        self._ensure_connected()
        if self._last_source is None:
            raise ValueError("forget() called before backup() — _last_source is not set")
        self._invoke(
            "policy",
            "set",
            self._last_source,
            f"--keep-daily={daily}",
            f"--keep-weekly={weekly}",
            f"--keep-monthly={monthly}",
        )
        if prune:
            self._invoke("maintenance", "run")

    def unlock(self) -> None:
        # Kopia does not use explicit restic-style repository locks,
        # but running maintenance clears any stale connections.
        self._ensure_connected()
        self._invoke("maintenance", "run", "--force")

    def check(self) -> None:
        self._ensure_connected()
        self._invoke("snapshot", "verify", "--sources=all")

    def verify_snapshot(self, run_tag: str) -> SnapshotInfo:
        self._ensure_connected()
        raw = self._invoke("snapshot", "list", "--json")
        data = self._loads_list(raw)
        matches = [s for s in data if self._has_tags(s, [run_tag])]
        if len(matches) == 0:
            raise BackupError(f"no snapshot found with tag {run_tag!r}")
        if len(matches) > 1:
            raise BackupError(f"ambiguous: {len(matches)} snapshots found with tag {run_tag!r}")
        snap = matches[0]
        snap_id = snap["id"]
        size = self.snapshot_size(snap_id)
        return SnapshotInfo(id=snap_id, short_id=snap_id, size_bytes=size)

    # ── internal ───────────────────────────────────────────────────────────────

    def _invoke(self, *args: str) -> str:
        log.debug("kopia %s", " ".join(args))
        try:
            result = self._k(*args)
            output = str(result)
            for line in output.splitlines():
                if line.strip():
                    log.info("kopia: %s", line)
            return output
        except sh.ErrorReturnCode as e:
            # _err_to_out merges kopia's error output into stdout; read both
            raw_out = e.stdout
            raw_err = e.stderr
            parts = [
                raw if isinstance(raw, str) else raw.decode(errors="replace")
                for raw in (raw_out, raw_err)
            ]
            stderr = "\n".join(p for p in parts if p).strip()
            log.error("kopia error: %s", stderr)
            if "lock" in stderr.lower():
                # Typed so the retry logic catches the condition, not the string.
                raise RepositoryLockedError(
                    f"kopia exited {e.exit_code}", e.exit_code, stderr
                ) from e
            raise BackupError(f"kopia exited {e.exit_code}", e.exit_code, stderr) from e

    @staticmethod
    def _loads_list(raw: str) -> list:
        """Parse a JSON array from kopia output that may interleave log lines
        with the JSON (stderr is merged into stdout via _err_to_out)."""
        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end == -1 or end < start:
            raise ValueError(f"no JSON array in kopia output: {raw[:200]!r}")
        data: list = json.loads(raw[start : end + 1])
        return data
