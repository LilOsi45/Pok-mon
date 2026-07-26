"""Keep the database from growing without bound.

Every check writes a stock_checks row. At 40 watches on a 2-minute interval
that is ~29 000 rows a day — about 2 GB and 10 million rows a year, on a VPS
disk, with the heartbeat and the price history scanning that table.

Nothing here touches scan_items: those rows ARE the scanner's memory of what it
has already seen. Deleting them would make every known product look new again
and fire a flood of false alerts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Notification, StockCheck, utcnow

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PruneResult:
    checks_deleted: int
    notifications_deleted: int

    @property
    def total(self) -> int:
        return self.checks_deleted + self.notifications_deleted


async def prune_old_rows(session: AsyncSession) -> PruneResult:
    """Delete history older than the configured retention. Commits."""
    settings = get_settings()
    now = utcnow()

    checks_cutoff = now - timedelta(days=settings.check_retention_days)
    notes_cutoff = now - timedelta(days=settings.notification_retention_days)

    checks = await session.execute(
        delete(StockCheck).where(StockCheck.checked_at < checks_cutoff)
    )
    notes = await session.execute(
        delete(Notification).where(Notification.created_at < notes_cutoff)
    )
    await session.commit()

    result = PruneResult(checks.rowcount or 0, notes.rowcount or 0)
    if result.total:
        log.info(
            "retention: removed %d stock checks (older than %dd) and %d notifications "
            "(older than %dd)",
            result.checks_deleted,
            settings.check_retention_days,
            result.notifications_deleted,
            settings.notification_retention_days,
        )
    return result


async def row_counts(session: AsyncSession) -> dict[str, int]:
    """Current size of the two growing tables — used by the report tool."""
    return {
        "stock_checks": (await session.scalar(select(func.count(StockCheck.id)))) or 0,
        "notifications": (await session.scalar(select(func.count(Notification.id)))) or 0,
    }


async def prune_job() -> None:
    """Scheduler entry point — own session."""
    from app.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        await prune_old_rows(session)
