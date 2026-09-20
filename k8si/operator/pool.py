"""Dedicated bounded executor and backup pacing knobs for the operator.

The backup pipeline parks threads for minutes at a time — job completion waits
up to jobTimeout (default 1h), the snapshot-conflict wait up to 30 minutes.
Running those on asyncio's shared default executor (ThreadPoolExecutor, sized
to the CPU count) lets a post-restart catch-up batch of concurrent backups
occupy every worker, and then even the timers' own k8s calls queue behind
hour-long sleeps: event loop idle, nothing progressing, operator frozen — the
recorded scheduler-hang bug. All such waits run on this dedicated bounded
executor instead, and run_backup itself is additionally capped by a semaphore.

Pacing knobs (env; deploy/configmap.yaml is the intended source):
- K8SI_MAX_CONCURRENT_BACKUPS — backups executing at once (default 2;
  1 fully serializes them).
- K8SI_BACKUP_SETTLE_SECONDS — quiet time between the end of one backup
  and the start of the next (default 0 = off). On storage that needs to
  drain between heavy Jobs (cache flush, SMR) this spaces the queue out.
"""

import asyncio
import concurrent.futures
import logging
import os
import time


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """Parse an int env var; absent or garbage falls back to *default*."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


MAX_CONCURRENT_BACKUPS = _env_int("K8SI_MAX_CONCURRENT_BACKUPS", default=2)
BACKUP_SETTLE_SECONDS = _env_int("K8SI_BACKUP_SETTLE_SECONDS", default=0, minimum=0)

EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    # Every running backup parks one worker for the whole job duration, plus
    # headroom for the snapshot-conflict and hook waits — scale with the cap.
    max_workers=max(4, MAX_CONCURRENT_BACKUPS + 2),
    thread_name_prefix="k8si-backup",
)

SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_BACKUPS)

_last_finish_monotonic: float = 0.0


async def settle_gap(logger: logging.Logger | None = None) -> None:
    """Sleep out the remaining settle time since the last finished backup.

    Called while already holding the semaphore, just before the next backup
    starts — during the wait the run honestly stays in phase Queued.
    """
    global _last_finish_monotonic
    remaining = _last_finish_monotonic + BACKUP_SETTLE_SECONDS - time.monotonic()
    if remaining <= 0:
        return
    if logger is not None:
        logger.info(
            "Settling %.0fs before next backup (K8SI_BACKUP_SETTLE_SECONDS=%d)",
            remaining,
            BACKUP_SETTLE_SECONDS,
        )
    await asyncio.sleep(remaining)


def note_backup_finished() -> None:
    """Record the finish time the next backup's settle gap counts from."""
    global _last_finish_monotonic
    _last_finish_monotonic = time.monotonic()


async def to_pool(fn, *args):
    """asyncio.to_thread, but on the dedicated bounded executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(EXECUTOR, lambda: fn(*args))
