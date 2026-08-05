"""Alert while something is broken, not once a day.

A Pokémon Center scanner sat in an error state for days. Three things had to go
wrong together for that to stay invisible, and all three did:

  * the scanner watched the wrong URL, so it could never have found anything;
  * its failure was only mentioned in the daily heartbeat;
  * that heartbeat's route matched no notifier, so it was never delivered.

The drop was very nearly missed. A stock tracker that fails quietly is worse
than none, so a broken watch or scanner now raises its own alert within the
hour, repeated at most once per cooldown so it cannot become noise.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import Event
from app.models import EventType, ProductScan, Watch
from app.notify.service import dispatch_event

log = logging.getLogger(__name__)

BROKEN_AFTER_SECONDS = 3600.0  # tolerate an hour of trouble before shouting
REPEAT_AFTER_SECONDS = 6 * 3600.0
MAX_LISTED = 8

# key -> monotonic time of the last alert about it
_alerted: dict[str, float] = {}
# key -> when it started failing. A single last_error says nothing about how
# long it has been that way, and alerting on the first hiccup would train the
# operator to ignore the alarm.
_failing_since: dict[str, float] = {}


def reset_for_tests() -> None:
    _alerted.clear()
    _failing_since.clear()


def _due(key: str) -> bool:
    last = _alerted.get(key)
    return last is None or (time.monotonic() - last) >= REPEAT_AFTER_SECONDS


async def collect_broken(session: AsyncSession) -> list[tuple[str, str, str]]:
    """(key, label, error) for everything failing longer than the grace period."""
    watches = (await session.execute(select(Watch).where(Watch.enabled.is_(True)))).scalars().all()
    scans = (
        (await session.execute(select(ProductScan).where(ProductScan.enabled.is_(True))))
        .scalars()
        .all()
    )
    seen: dict[str, tuple[str, str]] = {}
    for w in watches:
        if w.last_error:
            seen[f"watch:{w.id}"] = (f"Watch „{w.label}“", w.last_error)
    for s in scans:
        if s.last_error:
            seen[f"scan:{s.id}"] = (f"Scanner „{s.label}“", s.last_error)

    now = time.monotonic()
    for key in list(_failing_since):
        if key not in seen:  # recovered — forget it, including its alert history
            _failing_since.pop(key, None)
            _alerted.pop(key, None)
    broken = []
    for key, (label, error) in seen.items():
        since = _failing_since.setdefault(key, now)
        if now - since >= BROKEN_AFTER_SECONDS:
            broken.append((key, label, error))
    return broken


async def build_alarm(session: AsyncSession) -> Event | None:
    """One alert for everything newly broken, or None when there is nothing new."""
    broken = await collect_broken(session)
    fresh = [item for item in broken if _due(item[0])]
    if not fresh:
        return None
    for key, _label, _error in fresh:
        _alerted[key] = time.monotonic()

    lines = [f"{len(broken)} Watches/Scanner melden gerade einen Fehler:", ""]
    lines += [f"• {label}: {error[:100]}" for _key, label, error in fresh[:MAX_LISTED]]
    if len(fresh) > MAX_LISTED:
        lines.append(f"… und {len(fresh) - MAX_LISTED} weitere")
    lines.append("")
    lines.append("Prüfen mit:  docker compose exec -T app python -m app.health_report")
    return Event(
        type=EventType.HEARTBEAT,
        game=None,
        title="⚠️ Tracker: etwas hängt",
        message="\n".join(lines)[:3900],
        # Deliberately broad: an alarm that no channel receives is worthless,
        # and that is exactly how the missed drop stayed invisible.
        routes=["system:alarm", "system:heartbeat", "news:*"],
        # No @everyone. This is maintenance, not a drop: a broken watch is worth
        # reading at breakfast, not worth pulling everyone out of whatever they
        # are doing. Sharing the ping that means "buy now" with the one that
        # means "a shop is throttling us" is how an alert channel stops being
        # read — the exact failure this watchdog exists to prevent.
        priority=False,
    )


async def run_watchdog() -> None:
    """Scheduler entry point. Never raises."""
    from app.db import get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            event = await build_alarm(session)
            if event is not None:
                await dispatch_event(session, event)
    except Exception:
        log.exception("watchdog failed")
