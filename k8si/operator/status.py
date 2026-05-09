"""Status helpers for K8siBackup resources."""

from datetime import datetime, timezone

from croniter import croniter


def compute_next_backup(schedule: str) -> str:
    cron = croniter(schedule, datetime.now(tz=timezone.utc))
    return cron.get_next(datetime).isoformat()  # type: ignore[return-value]
