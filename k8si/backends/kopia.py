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

from ..backend import BackupError, NoSnapshotsError

log = logging.getLogger(__name__)


class KopiaBackend:
    """Wraps the kopia CLI via sh. All exceptions are converted to BackupError."""

    def __init__(self, env: dict[str, str]) -> None:
        self._config_file = env.get("KOPIA_CONFIG_PATH", "/tmp/kopia.config")
        self._repo = env.get("RESTIC_REPOSITORY", "")
        # Pass password via env var (not CLI arg) to avoid leaking it in /proc/*/cmdline
        self._env = dict(env)
        self._env["KOPIA_PASSWORD"] = env.get("RESTIC_PASSWORD") or env.get("KOPIA_PASSWORD", "")
        self._k = sh.kopia.bake(
            "--config-file",
            self._config_file,
            _env=self._env,
            _encoding="utf-8",
        )
        self._connected = False
        self._last_source: str | None = None

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
            path = self._repo.replace("file://", "")
            args.extend(["filesystem", f"--path={path}"])

        try:
            self._invoke(*args)
            self._connected = True
        except BackupError as e:
            if (
                "repository not initialized" in e.stderr.lower()
                or "cannot open" in e.stderr.lower()
            ):
                raise BackupError("repository does not exist", e.returncode, e.stderr) from e
            raise

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
            path = self._repo.replace("file://", "")
            args.extend(["filesystem", f"--path={path}"])

        self._invoke(*args)
        self._connected = True

    def snapshots(self, tags: list[str] | None = None) -> list[dict]:
        self._ensure_connected()
        raw = self._invoke("snapshot", "list", "--json")
        data = json.loads(raw.strip() or "[]")

        # Map Kopia format to match restic return format: [ {id, short_id, time} ]
        results = []
        for snap in data:
            results.append(
                {
                    "id": snap["id"],
                    "short_id": snap["id"][:8],
                    "time": snap["startTime"],
                }
            )
        return results

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
        "/data/foo" (flat layout), and "data/foo" (bare).
        """
        self._ensure_connected()
        if not sentinels:
            return True

        candidates: dict[str, set[str]] = {s: {f"/data/{s}", f"/{s}", s} for s in sentinels}
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
                    if path in candidates[sentinel]:
                        unfound.discard(sentinel)
            _, stderr = proc.communicate()
            rc = proc.returncode
            if rc not in (0, -9) and unfound:
                raise BackupError(f"kopia ls exited {rc}", rc, stderr.strip())

        return not unfound

    def snapshot_size(self, snapshot_id: str) -> int:
        self._ensure_connected()
        raw = self._invoke("snapshot", "show", "--json", snapshot_id)
        try:
            data = json.loads(raw)
            # Root entry stats size
            return int(data.get("rootEntry", {}).get("summ", {}).get("size", 0))
        except (json.JSONDecodeError, KeyError, TypeError):
            return 0

    def restore(self, snapshot_id: str = "latest") -> None:
        self._ensure_connected()
        try:
            self._invoke("snapshot", "restore", snapshot_id, "/")
        except BackupError as e:
            if "not found" in e.stderr.lower():
                raise NoSnapshotsError("no snapshots in repository", e.returncode, e.stderr) from e
            raise

    def backup(self, source: Path, tags: list[str] | None = None) -> None:
        self._ensure_connected()
        self._last_source = str(source)  # store for forget()
        # Set tags if specified (Kopia tags are set per source policy or on snapshot)
        args = ["snapshot", "create", str(source)]
        if tags:
            for tag in tags:
                args.extend(["--tags", tag])
        self._invoke(*args)

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
        self._invoke("snapshot", "verify", "--all")

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
            raw_stderr = e.stderr
            stderr = (
                raw_stderr if isinstance(raw_stderr, str) else raw_stderr.decode(errors="replace")
            ).strip()
            log.error("kopia error: %s", stderr)
            raise BackupError(f"kopia exited {e.exit_code}", e.exit_code, stderr) from e
