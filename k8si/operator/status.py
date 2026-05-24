"""Status helpers for K8siBackup resources."""

from datetime import UTC, datetime

from croniter import croniter


def compute_next_backup(schedule: str) -> str:
    cron = croniter(schedule, datetime.now(tz=UTC))
    return cron.get_next(datetime).isoformat()  # type: ignore[no-any-return]
