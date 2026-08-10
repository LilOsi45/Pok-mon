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
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import Event
from app.models import EventType, ProductScan, StockCheck, Watch, utcnow
from app.notify.service import dispatch_event

log = logging.getLogger(__name__)

BROKEN_AFTER_SECONDS = 3600.0  # tolerate an hour of trouble before shouting
REPEAT_AFTER_SECONDS = 6 * 3600.0
MAX_LISTED = 8

# "news:*" used to be in here as insurance against an alarm nobody receives —
# the failure that hid the missed drop. It stopped being insurance and became
# noise: the staff notifier listens on system:*, so every warning also landed in
# the set-news channel next to actual set announcements. Unroutable events are
# now logged as errors and stored as failed notifications, and route_check
# verifies a receiver exists, so the gap is covered properly.
ALARM_ROUTES: tuple[str, ...] = ("system:alarm", "system:heartbeat")

# key -> monotonic time of the last alert about it
_alerted: dict[str, float] = {}
# key -> when it started failing. A single last_error says nothing about how
# long it has been that way, and alerting on the first hiccup would train the
# operator to ignore the alarm.
_failing_since: dict[str, float] = {}
# key -> when it last looked healthy. A shop under a 900 s cooldown alternates
# between failing and passing, and forgetting a watch's alert history on the
# first successful check re-armed the alarm every couple of hours. The same
# single watch reported at 01:11, 03:48 and again later, about a shop that
# refuses us through the proxy too — nothing the operator can act on at night.
_recovered_at: dict[str, float] = {}
STABLE_FOR_SECONDS = 2 * 3600.0  # healthy this long before the alarm re-arms


def reset_for_tests() -> None:
    _alerted.clear()
    _failing_since.clear()
    _recovered_at.clear()


def _due(key: str) -> bool:
    last = _alerted.get(key)
    return last is None or (time.monotonic() - last) >= REPEAT_AFTER_SECONDS


async def _succeeded_recently(session: AsyncSession) -> set[int]:
    """Watch ids with at least one successful check inside the grace period."""
    since = utcnow() - timedelta(seconds=BROKEN_AFTER_SECONDS)
    rows = await session.execute(
        select(StockCheck.watch_id)
        .where(StockCheck.checked_at >= since, StockCheck.error.is_(None))
        .distinct()
    )
    return set(rows.scalars().all())


async def collect_broken(session: AsyncSession) -> list[tuple[str, str, str]]:
    """(key, label, error) for everything failing longer than the grace period."""
    watches = (await session.execute(select(Watch).where(Watch.enabled.is_(True)))).scalars().all()
    scans = (
        (await session.execute(select(ProductScan).where(ProductScan.enabled.is_(True))))
        .scalars()
        .all()
    )
    # A watch that succeeded recently is not broken, whatever its last check
    # said. Sampling "is there an error right now" every 30 minutes reported two
    # shops as hanging at 05:10 over cooldowns of 100 and 123 seconds — pauses
    # the throttling already handles, on shops that answer 99 % of the time. The
    # honest question is when the watch last produced a reading at all.
    recent = await _succeeded_recently(session)
    seen: dict[str, tuple[str, str]] = {}
    for w in watches:
        if w.last_error and w.id not in recent:
            seen[f"watch:{w.id}"] = (f"Watch „{w.label}“", w.last_error)
    for s in scans:
        if s.last_error:
            seen[f"scan:{s.id}"] = (f"Scanner „{s.label}“", s.last_error)

    now = time.monotonic()
    for key in list(_failing_since):
        if key in seen:
            _recovered_at.pop(key, None)
            continue
        # One good check is not a recovery. A shop serving 503 behind a 900 s
        # cooldown passes now and then, and clearing the history on that first
        # pass restarted the whole "broken for an hour -> alert" cycle, so the
        # same unfixable watch pinged again every couple of hours all night.
        healthy_since = _recovered_at.setdefault(key, now)
        if now - healthy_since >= STABLE_FOR_SECONDS:
            _failing_since.pop(key, None)
            _alerted.pop(key, None)
            _recovered_at.pop(key, None)
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
        routes=list(ALARM_ROUTES),
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
