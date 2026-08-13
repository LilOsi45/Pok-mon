"""Watch incoming Discord messages and ping on keyword hits.

Everything else in this tracker polls a shop. Plenty of drops never appear on a
page we know about — they surface as somebody's message in a cook group, and by
the time a shop page reflects them the thing is gone. This is that half.

    DISCORD_BOT_TOKEN=…   in .env, then restart

Requirements the token alone does not cover, and each fails silently if missed:

  * the bot must be a member of every server whose channels are watched — it
    cannot read a server it was never invited to;
  * "Message Content Intent" must be enabled for the application in the Discord
    developer portal. Without it every message arrives with an empty body and
    nothing ever matches, with no error anywhere.

Matching is in keyword_match.py; this module is the plumbing around it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.config import get_settings
from app.events import Event
from app.keyword_match import matches, parse_rules
from app.models import EventType, Game, KeywordAlert, utcnow

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1500
# One drop is posted three times in a row in a lively channel. Suppress a repeat
# of the same alert on near-identical text, so the ping stays worth reading.
DUPLICATE_WINDOW_SECONDS = 300.0
_recent: dict[tuple[int, str], float] = {}


@dataclass(frozen=True)
class IncomingMessage:
    """Just the parts of a Discord message the rules look at."""

    content: str
    channel_id: str
    channel_name: str = ""
    author: str = ""
    guild: str = ""
    url: str | None = None


def _fingerprint(text: str) -> str:
    return " ".join((text or "").split())[:200].casefold()


def is_duplicate(alert_id: int, text: str, now: float) -> bool:
    key = (alert_id, _fingerprint(text))
    last = _recent.get(key)
    _recent[key] = now
    for old_key, seen in list(_recent.items()):
        if now - seen > DUPLICATE_WINDOW_SECONDS:
            _recent.pop(old_key, None)
    return last is not None and now - last <= DUPLICATE_WINDOW_SECONDS


def reset_duplicates() -> None:
    _recent.clear()


def build_event(alert: KeywordAlert, message: IncomingMessage) -> Event:
    where = message.channel_name or message.channel_id
    if message.guild:
        where = f"{message.guild} · {where}"
    body = message.content.strip()[:MAX_MESSAGE_CHARS]
    return Event(
        type=EventType.NEW_LISTING,
        game=Game.POKEMON,
        title=f"🔔 {alert.label}",
        message=f"{body}\n\n— {message.author or 'unbekannt'} in {where}",
        url=message.url,
        retailer=where[:120],
        routes=[f"channel:{tag}" for tag in (alert.channels or [])] or ["system:alarm"],
        priority=alert.priority,
    )


async def handle_message(session, message: IncomingMessage) -> list[Event]:
    """Every alert this message trips, already recorded. Sends nothing itself."""
    from app.notify.service import dispatch_event

    alerts = (
        (await session.execute(select(KeywordAlert).where(KeywordAlert.enabled.is_(True))))
        .scalars()
        .all()
    )
    fired: list[Event] = []
    for alert in alerts:
        rules = parse_rules(alert.positive, alert.negative, alert.ranges)
        if not matches(rules, message.content, message.channel_id):
            continue
        if is_duplicate(alert.id, message.content, asyncio.get_running_loop().time()):
            log.info("keyword alert %s: same message again, not repeating", alert.id)
            continue
        alert.hits = (alert.hits or 0) + 1
        alert.last_hit_at = utcnow()
        event = build_event(alert, message)
        fired.append(event)
        if alert.delay_seconds:
            await asyncio.sleep(alert.delay_seconds)
        await dispatch_event(session, event)
    if fired:
        await session.commit()
    return fired


async def run_listener() -> None:
    """Connect to Discord and feed every message through the alerts.

    Never raises: a missing token or library must not stop the tracker, whose
    shop watching does not depend on any of this.
    """
    settings = get_settings()
    if not settings.discord_bot_token:
        log.info("keyword pinger idle — DISCORD_BOT_TOKEN not set")
        return
    try:
        import discord
    except ImportError:
        log.error(
            "keyword pinger needs the discord.py package — "
            "install the 'pinger' extra and rebuild the image"
        )
        return

    from app.db import get_sessionmaker

    intents = discord.Intents.default()
    intents.message_content = True  # must also be enabled in the developer portal
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:  # pragma: no cover - needs a live connection
        log.info("keyword pinger connected as %s", client.user)

    @client.event
    async def on_message(msg) -> None:  # pragma: no cover - needs a live connection
        if client.user is not None and msg.author.id == client.user.id:
            return  # never react to our own pings
        incoming = IncomingMessage(
            content=msg.content or "",
            channel_id=str(getattr(msg.channel, "id", "")),
            channel_name=getattr(msg.channel, "name", "") or "",
            author=str(getattr(msg.author, "display_name", "") or msg.author),
            guild=getattr(getattr(msg, "guild", None), "name", "") or "",
            url=getattr(msg, "jump_url", None),
        )
        try:
            async with get_sessionmaker()() as session:
                await handle_message(session, incoming)
        except Exception:
            log.exception("keyword pinger failed on a message")

    try:
        await client.start(settings.discord_bot_token)
    except Exception:
        log.exception("keyword pinger could not connect")
