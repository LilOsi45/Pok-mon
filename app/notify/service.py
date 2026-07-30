"""Notification dispatch: route events to matching notifiers, persist history,
and enforce quiet hours (non-priority events are suppressed, not sent)."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings, load_file_config
from app.events import Event
from app.models import EventType, Notification, NotifierConfig
from app.notify.base import Notifier, NotifyError
from app.notify.discord import DiscordNotifier
from app.notify.email import EmailNotifier
from app.notify.telegram import TelegramNotifier

log = logging.getLogger(__name__)

NOTIFIER_CLASSES: dict[str, type[Notifier]] = {
    "discord": DiscordNotifier,
    "email": EmailNotifier,
    "telegram": TelegramNotifier,
}


def _build(name: str, type_: str, config: dict, routes: list[str]) -> Notifier | None:
    cls = NOTIFIER_CLASSES.get(type_)
    if cls is None:
        log.error("unknown notifier type %r for %r", type_, name)
        return None
    return cls(name, config, routes)


async def load_notifiers(session: AsyncSession) -> list[Notifier]:
    """Merge notifiers from config.yaml and the dashboard-managed DB table.

    DB entries win on name collisions so the dashboard can override the file.
    """
    notifiers: dict[str, Notifier] = {}
    for cfg in load_file_config().notifiers:
        if not cfg.enabled:
            continue
        if n := _build(cfg.name, cfg.type, cfg.config, cfg.routes):
            notifiers[cfg.name] = n
    rows = (await session.execute(select(NotifierConfig).where(NotifierConfig.enabled))).scalars()
    for row in rows:
        if n := _build(row.name, row.type, dict(row.config or {}), list(row.routes or ["*"])):
            notifiers[row.name] = n
    return list(notifiers.values())


def parse_quiet_hours(spec: str) -> tuple[int, int] | None:
    """Parse "22-7" into (start_hour, end_hour); None when unset/invalid."""
    spec = (spec or "").strip()
    if not spec:
        return None
    try:
        start_raw, end_raw = spec.split("-", 1)
        start, end = int(start_raw), int(end_raw)
    except ValueError:
        log.warning("invalid QUIET_HOURS %r — expected e.g. '22-7', ignoring", spec)
        return None
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        log.warning("invalid QUIET_HOURS %r — hours must be 0-23 and differ", spec)
        return None
    return start, end


def is_quiet_hour(spec: str, hour: int) -> bool:
    window = parse_quiet_hours(spec)
    if window is None:
        return False
    start, end = window
    if start < end:  # e.g. 13-15
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight, e.g. 22-7


def _quiet_now() -> bool:
    settings = get_settings()
    now = datetime.now(ZoneInfo(settings.timezone))
    return is_quiet_hour(settings.quiet_hours, now.hour)


async def dispatch_event(session: AsyncSession, event: Event) -> list[Notification]:
    """Send an event to every notifier whose routes match; persist each attempt."""
    records: list[Notification] = []
    notifiers = await load_notifiers(session)
    matching = [n for n in notifiers if n.matches(event)]
    if not matching:
        # A misrouted event used to vanish: logged at INFO (invisible at the
        # configured level) and never recorded, so the notification history
        # looked like "nothing happened". A daily heartbeat warning about a
        # broken Pokémon Center scanner disappeared that way for days, and the
        # drop it was meant to catch was missed. Make it loud and persistent.
        log.error(
            "event %s (%s) matched NO notifier — routes %s reach nobody",
            event.type.value,
            event.title,
            event.routes,
        )
        session.add(
            Notification(
                event_type=event.type,
                notifier="(kein Kanal)",
                notifier_type="none",
                title=event.title,
                body=event.message or None,
                url=event.url,
                watch_id=event.watch_id,
                news_id=event.news_id,
                success=False,
                error=f"kein Notifier passt zu {event.routes} — in config.yaml eintragen",
            )
        )
        await session.commit()
        return []

    # Quiet hours: suppress non-priority noise (still recorded in history).
    # Priority events, tests and the daily heartbeat always go through.
    if (
        matching
        and not event.priority
        and event.type not in (EventType.TEST, EventType.HEARTBEAT)
        and _quiet_now()
    ):
        for notifier in matching:
            session.add(
                Notification(
                    event_type=event.type,
                    notifier=notifier.name,
                    notifier_type=notifier.type,
                    title=event.title,
                    body=event.message or None,
                    url=event.url,
                    watch_id=event.watch_id,
                    news_id=event.news_id,
                    success=False,
                    error=f"unterdrückt (Ruhezeit {get_settings().quiet_hours})",
                )
            )
        await session.commit()
        log.info("event %s suppressed (quiet hours)", event.type.value)
        return []
    for notifier in matching:
        record = Notification(
            event_type=event.type,
            notifier=notifier.name,
            notifier_type=notifier.type,
            title=event.title,
            body=event.message or None,
            url=event.url,
            watch_id=event.watch_id,
            news_id=event.news_id,
            payload={
                "game": event.game.value if event.game else None,
                "retailer": event.retailer,
                "price": event.price,
                "currency": event.currency,
                "image_url": event.image_url,
                "routes": event.routes,
            },
        )
        try:
            await notifier.send(event)
            record.success = True
            log.info(
                "sent %s via %s: %s",
                event.type.value,
                notifier.name,
                event.title,
                extra={"event": event.type.value},
            )
        except NotifyError as exc:
            record.success = False
            record.error = str(exc)
            log.warning("notifier %s failed: %s", notifier.name, exc)
        except Exception as exc:  # never let one notifier break the pipeline
            record.success = False
            record.error = f"unexpected: {exc}"
            log.exception("notifier %s crashed", notifier.name)
        session.add(record)
        records.append(record)
    await session.commit()
    return records
