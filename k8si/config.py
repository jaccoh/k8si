"""Environment variable configuration with clear validation errors."""

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass
class Config:
    mode: str
    data_path: Path
    restic_repository: str
    restic_password: str | None
    restic_password_file: Path | None
    sentinel_file: str | None
    backup_schedule: str | None
    retention_daily: int
    retention_weekly: int
    retention_monthly: int
    pre_backup_hook: Path | None
    backup_tags: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Config":
        mode = _require("MODE", "restore | backup | job")
        if mode not in ("restore", "backup", "job"):
            raise ConfigError(f"MODE must be 'restore', 'backup', or 'job', got: {mode!r}")

        data_path = Path(os.environ.get("DATA_PATH", "/data"))
        restic_repository = _require("RESTIC_REPOSITORY", "restic repository URL")

        restic_password = os.environ.get("RESTIC_PASSWORD")
        restic_password_file_str = os.environ.get("RESTIC_PASSWORD_FILE")
        restic_password_file = Path(restic_password_file_str) if restic_password_file_str else None

        if not restic_password and not restic_password_file:
            raise ConfigError(
                "Either RESTIC_PASSWORD or RESTIC_PASSWORD_FILE must be set"
            )

        sentinel_file = None
        backup_schedule = None
        pre_backup_hook = None
        backup_tags: list[str] = []

        if mode == "restore":
            sentinel_file = _require("SENTINEL_FILE", "path relative to DATA_PATH root")
        elif mode in ("backup", "job"):
            if mode == "backup":
                backup_schedule = _require("BACKUP_SCHEDULE", "cron expression, e.g. '0 * * * *'")
            hook_str = os.environ.get("PRE_BACKUP_HOOK")
            pre_backup_hook = Path(hook_str) if hook_str else None
            tags_str = os.environ.get("BACKUP_TAGS", "")
            backup_tags = [t.strip() for t in tags_str.split(",") if t.strip()]

        return cls(
            mode=mode,
            data_path=data_path,
            restic_repository=restic_repository,
            restic_password=restic_password,
            restic_password_file=restic_password_file,
            sentinel_file=sentinel_file,
            backup_schedule=backup_schedule,
            retention_daily=int(os.environ.get("RETENTION_DAILY", "7")),
            retention_weekly=int(os.environ.get("RETENTION_WEEKLY", "4")),
            retention_monthly=int(os.environ.get("RETENTION_MONTHLY", "3")),
            pre_backup_hook=pre_backup_hook,
            backup_tags=backup_tags,
        )


def _require(name: str, description: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required ({description})")
    return value
