"""Status helpers for K8siBackup resources."""

from datetime import datetime, timedelta, timezone

from croniter import croniter


def compute_next_backup(schedule: str) -> str:
    cron = croniter(schedule, datetime.now(tz=timezone.utc))
    return cron.get_next(datetime).isoformat()  # type: ignore[return-value]


def infer_result(
    last_schedule: datetime | None,
    last_success: datetime | None,
) -> str:
    if last_success is None:
        return "pending"
    if last_schedule is None:
        return "success"
    # Last scheduled run hasn't completed successfully — likely failed
    if last_schedule > last_success + timedelta(minutes=30):
        return "failed"
    return "success"
