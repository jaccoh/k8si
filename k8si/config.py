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
    backend_type: str = "restic"

    # restore
    restore_sentinels: list[str] = field(default_factory=list)
    restore_required: bool = False
    restore_max_age_hours: float | None = None
    restore_size_min: int | None = None  # bytes
    restore_size_max: int | None = None  # bytes
    restore_tags: list[str] = field(default_factory=list)
    restore_snapshot: str | None = None

    # restore reporting (opt-in)
    backup_name: str | None = None
    backup_namespace: str | None = None

    # backup / job
    backup_schedule: str | None = None
    retention_daily: int = 7
    retention_weekly: int = 4
    retention_monthly: int = 3
    pre_snapshot_hook: Path | None = None
    pre_snapshot_hook_required: bool = False
    backup_tags: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Config":
        mode = _require("MODE", "restore | backup | job")
        if mode not in ("restore", "backup", "job"):
            raise ConfigError(f"MODE must be 'restore', 'backup', or 'job', got: {mode!r}")

        backend_type = os.environ.get("BACKEND_TYPE", "restic").lower().strip()
        if backend_type not in ("restic", "kopia"):
            raise ConfigError(f"BACKEND_TYPE must be 'restic' or 'kopia', got: {backend_type!r}")

        data_path = Path(os.environ.get("DATA_PATH", "/data"))
        restic_repository = _require("RESTIC_REPOSITORY", "restic repository URL")

        restic_password = os.environ.get("RESTIC_PASSWORD")
        restic_password_file_str = os.environ.get("RESTIC_PASSWORD_FILE")
        restic_password_file = Path(restic_password_file_str) if restic_password_file_str else None

        if not restic_password and not restic_password_file:
            raise ConfigError("Either RESTIC_PASSWORD or RESTIC_PASSWORD_FILE must be set")

        restore_sentinels: list[str] = []
        restore_required = False
        restore_max_age_hours: float | None = None
        restore_size_min: int | None = None
        restore_size_max: int | None = None
        restore_tags: list[str] = []
        restore_snapshot: str | None = None
        backup_name: str | None = None
        backup_namespace: str | None = None
        backup_schedule: str | None = None
        pre_snapshot_hook: Path | None = None
        pre_snapshot_hook_required = False
        backup_tags: list[str] = []

        if mode == "restore":
            # RESTORE_SENTINELS preferred; fall back to SENTINEL_FILE for compat
            sentinels_str = os.environ.get("RESTORE_SENTINELS", "")
            if sentinels_str:
                restore_sentinels = [s.strip() for s in sentinels_str.split(",") if s.strip()]
            else:
                sf = os.environ.get("SENTINEL_FILE", "").strip()
                restore_sentinels = [sf] if sf else []

            restore_required = os.environ.get("RESTORE_REQUIRED", "false").lower() == "true"

            if age_str := os.environ.get("RESTORE_MAX_AGE", ""):
                restore_max_age_hours = _parse_duration_hours(age_str)

            if min_str := os.environ.get("RESTORE_SIZE_MIN", ""):
                restore_size_min = _parse_bytes(min_str)

            if max_str := os.environ.get("RESTORE_SIZE_MAX", ""):
                restore_size_max = _parse_bytes(max_str)

            tags_str = os.environ.get("RESTORE_TAGS", "")
            restore_tags = [t.strip() for t in tags_str.split(",") if t.strip()]

            restore_snapshot = os.environ.get("RESTORE_SNAPSHOT", "").strip() or None

            backup_name = os.environ.get("K8SI_BACKUP_NAME", "").strip() or None
            backup_namespace = os.environ.get("K8SI_BACKUP_NAMESPACE", "").strip() or None

        elif mode in ("backup", "job"):
            if mode == "backup":
                backup_schedule = _require("BACKUP_SCHEDULE", "cron expression, e.g. '0 * * * *'")
            hook_str = os.environ.get("PRE_SNAPSHOT_HOOK")
            pre_snapshot_hook = Path(hook_str) if hook_str else None
            pre_snapshot_hook_required = (
                os.environ.get("PRE_SNAPSHOT_HOOK_REQUIRED", "false").lower() == "true"
            )
            tags_str = os.environ.get("BACKUP_TAGS", "")
            backup_tags = [t.strip() for t in tags_str.split(",") if t.strip()]

        return cls(
            mode=mode,
            data_path=data_path,
            restic_repository=restic_repository,
            restic_password=restic_password,
            restic_password_file=restic_password_file,
            backend_type=backend_type,
            restore_sentinels=restore_sentinels,
            restore_required=restore_required,
            restore_max_age_hours=restore_max_age_hours,
            restore_size_min=restore_size_min,
            restore_size_max=restore_size_max,
            restore_tags=restore_tags,
            restore_snapshot=restore_snapshot,
            backup_name=backup_name,
            backup_namespace=backup_namespace,
            backup_schedule=backup_schedule,
            retention_daily=int(os.environ.get("RETENTION_DAILY", "7")),
            retention_weekly=int(os.environ.get("RETENTION_WEEKLY", "4")),
            retention_monthly=int(os.environ.get("RETENTION_MONTHLY", "3")),
            pre_snapshot_hook=pre_snapshot_hook,
            pre_snapshot_hook_required=pre_snapshot_hook_required,
            backup_tags=backup_tags,
        )


def _require(name: str, description: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required ({description})")
    return value


def _parse_duration_hours(s: str) -> float:
    s = s.strip().lower()
    if s.endswith("d"):
        return float(s[:-1]) * 24
    if s.endswith("h"):
        return float(s[:-1])
    if s.endswith("m"):
        return float(s[:-1]) / 60
    raise ConfigError(f"Cannot parse duration: {s!r} (expected e.g. '7d', '168h', '30m')")


def _parse_bytes(s: str) -> int:
    s = s.strip()
    for suffix, mult in [
        ("Ti", 1024**4),
        ("Gi", 1024**3),
        ("Mi", 1024**2),
        ("Ki", 1024),
        ("T", 1000**4),
        ("G", 1000**3),
        ("M", 1000**2),
        ("K", 1000),
    ]:
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * mult)
    return int(s)
