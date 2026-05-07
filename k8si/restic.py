"""Thin restic subprocess wrapper with typed errors."""

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class ResticError(Exception):
    def __init__(self, message: str, returncode: int, stderr: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class ResticNoSnapshotsError(ResticError):
    pass


class Restic:
    def __init__(self, env: dict[str, str]) -> None:
        self._env = env
        sftp_cmd = env.get("RESTIC_SFTP_COMMAND")
        self._global_opts = ["-o", f"sftp.command={sftp_cmd}"] if sftp_cmd else []

    def _cmd(self, *args: str) -> list[str]:
        return ["restic"] + self._global_opts + list(args)

    def init(self) -> None:
        self._run(self._cmd("init"))

    def restore(self) -> None:
        try:
            self._run(self._cmd("restore", "latest", "--target", "/"))
        except ResticError as e:
            if "no matching snapshot" in e.stderr or "no snapshots found" in e.stderr:
                raise ResticNoSnapshotsError(
                    "no snapshots in repository", e.returncode, e.stderr
                ) from e
            raise

    def backup(self, source: Path, tags: list[str] | None = None) -> None:
        cmd = self._cmd("backup", str(source))
        for tag in tags or []:
            cmd += ["--tag", tag]
        self._run(cmd)

    def forget(self, daily: int, weekly: int, monthly: int, prune: bool = True) -> None:
        cmd = self._cmd(
            "forget",
            "--keep-daily", str(daily),
            "--keep-weekly", str(weekly),
            "--keep-monthly", str(monthly),
        )
        if prune:
            cmd.append("--prune")
        self._run(cmd)

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        log.debug("restic: %s", " ".join(cmd[1:]))
        result = subprocess.run(
            cmd,
            env=self._env,
            capture_output=True,
            text=True,
        )
        if result.stdout:
            for line in result.stdout.splitlines():
                log.info("restic: %s", line)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            log.error("restic stderr: %s", stderr)
            raise ResticError(
                f"restic exited {result.returncode}",
                result.returncode,
                stderr,
            )
        return result
