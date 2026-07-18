"""Notification dispatch: route events to matching notifiers, persist history."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import load_file_config
from app.events import Event
from app.models import Notification, NotifierConfig
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


async def dispatch_event(session: AsyncSession, event: Event) -> list[Notification]:
    """Send an event to every notifier whose routes match; persist each attempt."""
    records: list[Notification] = []
    notifiers = await load_notifiers(session)
    matching = [n for n in notifiers if n.matches(event)]
    if not matching:
        log.info("event %s (%s) matched no notifiers", event.type.value, event.title)
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
