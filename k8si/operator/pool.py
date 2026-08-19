"""Dedicated bounded executor for the operator's long blocking Kubernetes polls.

The backup pipeline parks threads for minutes at a time — job completion waits
up to jobTimeout (default 1h), the snapshot-conflict wait up to 30 minutes.
Running those on asyncio's shared default executor (ThreadPoolExecutor, sized
to the CPU count) lets a post-restart catch-up batch of concurrent backups
occupy every worker, and then even the timers' own k8s calls queue behind
hour-long sleeps: event loop idle, nothing progressing, operator frozen — the
recorded scheduler-hang bug. All such waits run on this dedicated bounded
executor instead, and run_backup itself is additionally capped by a semaphore.
"""

import asyncio
import concurrent.futures

EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="k8si-backup")

MAX_CONCURRENT_BACKUPS = 2
SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_BACKUPS)


async def to_pool(fn, *args):
    """asyncio.to_thread, but on the dedicated bounded executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(EXECUTOR, lambda: fn(*args))
