"""Restic backend — implements BackupBackend using the sh library.

Drop-in replacement: swap this module for a kopia backend without changing
backup.py, restore.py, or any other caller.
"""

import json
import logging
import subprocess
from pathlib import Path

import sh

from ..backend import BackupError, NoSnapshotsError

log = logging.getLogger(__name__)


class ResticBackend:
    """Wraps the restic CLI via sh.  All sh.ErrorReturnCode exceptions are
    converted to BackupError so callers never depend on sh internals."""

    def __init__(self, env: dict[str, str]) -> None:
        self._env = env
        sftp_cmd = env.get("RESTIC_SFTP_COMMAND")
        self._global_opts: list[str] = ["-o", f"sftp.command={sftp_cmd}"] if sftp_cmd else []
        self._r = sh.restic.bake(*self._global_opts, _env=env, _encoding="utf-8")

    # ── BackupBackend protocol ─────────────────────────────────────────────────

    def init(self) -> None:
        self._invoke("init")

    def snapshots(self, tags: list[str] | None = None) -> list[dict]:
        tag_args = sum([["--tag", t] for t in (tags or [])], [])
        raw = self._invoke("snapshots", "--json", *tag_args)
        data = json.loads(raw.strip() or "[]")
        return data if isinstance(data, list) else []

    def ls(self, snapshot_id: str) -> list[str]:
        """Return file paths contained in a snapshot (metadata only, no data transfer)."""
        raw = self._invoke("ls", "--json", snapshot_id)
        paths = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("type") == "file":
                    paths.append(obj["path"])
            except (json.JSONDecodeError, KeyError):
                pass
        return paths

    def check_sentinels(self, snapshot_id: str, sentinels: list[str]) -> bool:
        """Stream restic ls line by line; return True iff all sentinels exist (files or dirs).

        Each sentinel like "data/foo" is matched against "/data/data/foo" (subPath layout),
        "/data/foo" (flat layout), and "data/foo" (bare). Exits the stream early once all
        sentinels are confirmed, avoiding full traversal of large snapshots.
        """
        if not sentinels:
            return True

        candidates: dict[str, set[str]] = {
            s: {f"/data/{s}", f"/{s}", s} for s in sentinels
        }
        unfound = set(sentinels)

        cmd = ["restic", "ls", "--json", snapshot_id, *self._global_opts]
        with subprocess.Popen(
            cmd, env=self._env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace",
        ) as proc:
            assert proc.stdout is not None
            for raw in proc.stdout:
                if not unfound:
                    proc.kill()
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    path = json.loads(raw).get("path", "")
                except json.JSONDecodeError:
                    continue
                for sentinel in list(unfound):
                    if path in candidates[sentinel]:
                        unfound.discard(sentinel)
            _, stderr = proc.communicate()
            rc = proc.returncode
            # rc == -9 means we killed it intentionally (all sentinels found early)
            if rc not in (0, -9) and unfound:
                raise BackupError(f"restic ls exited {rc}", rc, stderr.strip())

        if unfound:
            log.warning(
                "Sentinels not found in snapshot %s: %s", snapshot_id, sorted(unfound)
            )
            return False
        return True

    def snapshot_size(self, snapshot_id: str) -> int:
        """Return restore size in bytes (metadata only, no data transfer)."""
        raw = self._invoke("stats", "--json", snapshot_id)
        return json.loads(raw).get("total_size", 0)

    def restore(self, snapshot_id: str = "latest") -> None:
        try:
            self._invoke("restore", snapshot_id, "--target", "/")
        except BackupError as e:
            if "no matching snapshot" in e.stderr or "no snapshots found" in e.stderr:
                raise NoSnapshotsError(
                    "no snapshots in repository", e.returncode, e.stderr
                ) from e
            raise

    def backup(self, source: Path, tags: list[str] | None = None) -> None:
        tag_args = sum([["--tag", t] for t in (tags or [])], [])
        self._invoke("backup", str(source), *tag_args)

    def forget(self, daily: int, weekly: int, monthly: int, prune: bool = True) -> None:
        args = [
            "forget",
            "--keep-daily", str(daily),
            "--keep-weekly", str(weekly),
            "--keep-monthly", str(monthly),
        ]
        if prune:
            args.append("--prune")
        self._invoke(*args)

    # ── internal ───────────────────────────────────────────────────────────────

    def _invoke(self, *args: str) -> str:
        log.debug("restic %s", " ".join(args))
        try:
            result = self._r(*args)
            output = str(result)
            for line in output.splitlines():
                if line.strip():
                    log.info("restic: %s", line)
            return output
        except sh.ErrorReturnCode as e:
            raw_stderr = e.stderr
            stderr = (
                raw_stderr
                if isinstance(raw_stderr, str)
                else raw_stderr.decode(errors="replace")
            ).strip()
            log.error("restic error: %s", stderr)
            raise BackupError(
                f"restic exited {e.exit_code}", e.exit_code, stderr
            ) from e
