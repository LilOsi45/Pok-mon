"""Daily heartbeat: an "I'm alive" status message with 24h stats.

The worst failure mode of an alerting system is silent death — no restocks
looks exactly like a crashed tracker. The heartbeat proves liveness once a day
and surfaces watches/scanners that are stuck in an error state.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import Event
from app.models import (
    EventType,
    Notification,
    ProductScan,
    ScanItem,
    SetNews,
    StockCheck,
    Watch,
    utcnow,
)
from app.notify.service import dispatch_event

log = logging.getLogger(__name__)


async def build_heartbeat_event(session: AsyncSession) -> Event:
    since = utcnow() - timedelta(hours=24)

    async def count(query) -> int:
        return (await session.execute(query)).scalar() or 0

    checks = await count(select(func.count(StockCheck.id)).where(StockCheck.checked_at >= since))
    check_errors = await count(
        select(func.count(StockCheck.id)).where(
            StockCheck.checked_at >= since, StockCheck.error.is_not(None)
        )
    )
    sent = await count(
        select(func.count(Notification.id)).where(
            Notification.created_at >= since, Notification.success.is_(True)
        )
    )
    news = await count(select(func.count(SetNews.id)).where(SetNews.first_seen >= since))
    scan_hits = await count(select(func.count(ScanItem.id)).where(ScanItem.first_seen >= since))
    watches_active = await count(select(func.count(Watch.id)).where(Watch.enabled.is_(True)))
    scans_active = await count(
        select(func.count(ProductScan.id)).where(ProductScan.enabled.is_(True))
    )

    failing_watches = (
        (
            await session.execute(
                select(Watch).where(Watch.enabled.is_(True), Watch.last_error.is_not(None)).limit(5)
            )
        )
        .scalars()
        .all()
    )
    failing_scans = (
        (
            await session.execute(
                select(ProductScan)
                .where(ProductScan.enabled.is_(True), ProductScan.last_error.is_not(None))
                .limit(5)
            )
        )
        .scalars()
        .all()
    )

    lines = [
        f"**Letzte 24 h:** {checks} Checks · {check_errors} Fehler · "
        f"{sent} Benachrichtigungen · {news} News · {scan_hits} Scanner-Treffer",
        f"**Aktiv:** {watches_active} Watches · {scans_active} Scanner",
    ]
    problems = [f"Watch „{w.label}“: {(w.last_error or '')[:80]}" for w in failing_watches]
    problems += [f"Scanner „{s.label}“: {(s.last_error or '')[:80]}" for s in failing_scans]
    if problems:
        lines.append("")
        lines.append("⚠️ **Hängt gerade:**")
        lines.extend(f"• {p}" for p in problems)

    healthy = not problems
    return Event(
        type=EventType.HEARTBEAT,
        game=None,
        title="Tracker läuft" if healthy else "Tracker läuft — mit Problemen",
        message="\n".join(lines)[:3900],
        routes=["system:heartbeat"],
    )


async def send_heartbeat() -> None:
    """Scheduler entry point."""
    from app.db import get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            event = await build_heartbeat_event(session)
            await dispatch_event(session, event)
    except Exception:
        log.exception("heartbeat failed")
