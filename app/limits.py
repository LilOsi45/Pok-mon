"""Global ceiling on how many scheduled checks run at the same time.

APScheduler's asyncio executor has no worker limit: every due job starts
immediately. With dozens of watches on short intervals and fetches that can
take 30 s+ (the unlocker), that meant a large burst of concurrent checks —
which exhausted the DB connection pool and took the dashboard down with
"QueuePool limit of size 5 overflow 10 reached".

Checks now also release their DB connection before fetching, so this is the
second line of defence: it also keeps memory and outbound sockets bounded.
"""

from __future__ import annotations

import asyncio

from app.config import get_settings

_slots: asyncio.Semaphore | None = None


def job_slots() -> asyncio.Semaphore:
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(get_settings().scheduler_max_workers)
    return _slots


def reset_for_tests() -> None:
    global _slots
    _slots = None
