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
from app.keyword_match import Rules, filter_text, first_hit, matches, parse_rules
from app.models import BlockedUrl, EventType, Game, KeywordAlert, utcnow

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1500
BLOCK_PREFIX = "holo:block:"
# One drop is posted three times in a row in a lively channel. Suppress a repeat
# of the same alert on near-identical text, so the ping stays worth reading.
DUPLICATE_WINDOW_SECONDS = 300.0
_recent: dict[tuple[int, str], float] = {}


@dataclass(frozen=True)
class IncomingMessage:
    """Just the parts of a Discord message the alerts look at.

    `embed_text` matters as much as `content`: monitor bots put the product
    name, the price and the shop inside an embed and leave the message body
    empty, so matching only on `content` would never fire on exactly the
    messages worth having. `links` are the shop URLs found in the message,
    which is what the block list works on.
    """

    content: str
    channel_id: str
    channel_name: str = ""
    author: str = ""
    guild: str = ""
    url: str | None = None
    embed_text: str = ""
    links: tuple[str, ...] = ()

    @property
    def searchable(self) -> str:
        return f"{self.content}\n{self.embed_text}".strip()


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


def headline(alert: KeywordAlert, rules: Rules, hit: str | None, mention: str = "") -> str:
    """The line above the forwarded listing.

    Names the keyword that fired, the setting it belongs to and the filter in
    force. With a dozen keywords in one setting, a ping that does not say which
    one hit leaves you working it out from the product name.
    """
    parts = [f"{mention} ".lstrip() + "dein Keyword wurde gepingt:"]
    parts.append(f"**({hit})**" if hit else "**(?)**")
    parts.append(f'in Setting: **"{alert.label}"**')
    if active := filter_text(rules):
        parts.append(f"mit Filter: **{active}**")
    return " ".join(parts)


def build_event(alert: KeywordAlert, message: IncomingMessage, hit: str | None = None) -> Event:
    where = message.channel_name or message.channel_id
    if message.guild:
        where = f"{message.guild} · {where}"
    body = (message.searchable or "").strip()[:MAX_MESSAGE_CHARS]
    return Event(
        type=EventType.NEW_LISTING,
        game=Game.POKEMON,
        title=f"🔔 {alert.label}" + (f" — {hit}" if hit else ""),
        message=f"{body}\n\n— {message.author or 'unbekannt'} in {where}",
        url=message.url,
        retailer=where[:120],
        routes=[f"channel:{tag}" for tag in (alert.channels or [])] or ["system:alarm"],
        priority=alert.priority,
    )


async def blocked(session, links: tuple[str, ...]) -> bool:
    """Has one of these links been silenced with the Block URL button?"""
    if not links:
        return False
    found = await session.execute(select(BlockedUrl.url).where(BlockedUrl.url.in_(links)))
    return bool(found.first())


async def handle_message(session, message: IncomingMessage, *, with_alerts: bool = False) -> list:
    """Every alert this message trips, recorded and routed.

    Returns the events, or (alert, event) pairs when the caller also needs the
    alert — the direct message is addressed per alert.
    """
    from app.notify.service import dispatch_event

    alerts = (
        (await session.execute(select(KeywordAlert).where(KeywordAlert.enabled.is_(True))))
        .scalars()
        .all()
    )
    if await blocked(session, message.links):
        log.info("keyword pinger: link is on the block list, staying quiet")
        return []
    fired: list[Event] = []
    text = message.searchable
    for alert in alerts:
        rules = parse_rules(alert.positive, alert.negative, alert.ranges)
        if not matches(rules, text, message.channel_id):
            continue
        if is_duplicate(alert.id, text, asyncio.get_running_loop().time()):
            log.info("keyword alert %s: same message again, not repeating", alert.id)
            continue
        alert.hits = (alert.hits or 0) + 1
        alert.last_hit_at = utcnow()
        hit = first_hit(rules, text)
        event = build_event(alert, message, hit)
        event.message = headline(alert, rules, hit) + "\n\n" + event.message
        fired.append((alert, event) if with_alerts else event)
        if alert.delay_seconds:
            await asyncio.sleep(alert.delay_seconds)
        await dispatch_event(session, event)
    if fired:
        await session.commit()
    return fired


def embed_text_of(msg) -> tuple[str, tuple[str, ...]]:
    """Flatten a Discord message's embeds into text, and collect its links.

    Monitor bots put the product, the price and the shop in an embed and leave
    the message body empty. Matching only the body would miss exactly the
    messages worth having.
    """
    chunks: list[str] = []
    links: list[str] = []
    for embed in getattr(msg, "embeds", None) or []:
        for value in (
            getattr(embed, "title", None),
            getattr(embed, "description", None),
            getattr(getattr(embed, "author", None), "name", None),
            getattr(getattr(embed, "footer", None), "text", None),
        ):
            if value:
                chunks.append(str(value))
        if url := getattr(embed, "url", None):
            links.append(str(url))
            chunks.append(str(url))
        for field_ in getattr(embed, "fields", None) or []:
            name, value = getattr(field_, "name", ""), getattr(field_, "value", "")
            if name or value:
                chunks.append(f"{name} {value}".strip())
    return "\n".join(chunks), tuple(links)


def _view(jump_url: str | None, block_url: str | None):
    """The two buttons under a ping: open the original, or silence this link."""
    import discord

    view = discord.ui.View(timeout=None)
    if jump_url:
        view.add_item(
            discord.ui.Button(label="Go to Message", url=jump_url, style=discord.ButtonStyle.link)
        )
    if block_url:
        view.add_item(
            discord.ui.Button(
                label="Block URL",
                style=discord.ButtonStyle.danger,
                # The URL travels in the id so the handler needs no state of its
                # own and keeps working across restarts. Discord caps it at 100
                # characters, so a very long link simply gets no button rather
                # than a broken one.
                custom_id=f"{BLOCK_PREFIX}{block_url}"[:100],
            )
        )
    return view if view.children else None


async def block_url(session, url: str, label: str = "") -> bool:
    """Silence one link. Returns False when it was already on the list."""
    from app.models import BlockedUrl

    existing = await session.execute(select(BlockedUrl).where(BlockedUrl.url == url))
    if existing.scalars().first() is not None:
        return False
    session.add(BlockedUrl(url=url[:500], label=label[:500]))
    await session.commit()
    return True


async def send_dm(client, alert: KeywordAlert, event: Event, msg) -> None:
    """Deliver the hit as a direct message, forwarding the original embed."""
    user_id = (alert.dm_user_id or get_settings().discord_dm_user_id or "").strip()
    if not user_id:
        return
    try:
        user = client.get_user(int(user_id)) or await client.fetch_user(int(user_id))
    except Exception:
        log.exception("keyword pinger: cannot reach Discord user %s", user_id)
        return
    links = [
        u for u in (getattr(e, "url", None) for e in (getattr(msg, "embeds", None) or [])) if u
    ]
    view = _view(getattr(msg, "jump_url", None), links[0] if links else None)
    try:
        await user.send(
            content=event.message.split("\n\n")[0][:1900],
            embeds=list(getattr(msg, "embeds", None) or [])[:3],
            view=view,
        )
    except Exception:
        # A closed DM is the usual reason: the bot may only message someone who
        # shares a server with it and accepts DMs from server members.
        log.exception("keyword pinger: direct message to %s failed", user_id)


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
        text, links = embed_text_of(msg)
        incoming = IncomingMessage(
            content=msg.content or "",
            channel_id=str(getattr(msg.channel, "id", "")),
            channel_name=getattr(msg.channel, "name", "") or "",
            author=str(getattr(msg.author, "display_name", "") or msg.author),
            guild=getattr(getattr(msg, "guild", None), "name", "") or "",
            url=getattr(msg, "jump_url", None),
            embed_text=text,
            links=links,
        )
        try:
            async with get_sessionmaker()() as session:
                for alert, event in await handle_message(session, incoming, with_alerts=True):
                    await send_dm(client, alert, event, msg)
        except Exception:
            log.exception("keyword pinger failed on a message")

    @client.event
    async def on_interaction(interaction) -> None:  # pragma: no cover - needs Discord
        data = getattr(interaction, "data", None) or {}
        custom_id = str(data.get("custom_id") or "")
        if not custom_id.startswith(BLOCK_PREFIX):
            return
        url = custom_id[len(BLOCK_PREFIX) :]
        async with get_sessionmaker()() as session:
            added = await block_url(session, url)
        await interaction.response.send_message(
            f"Gesperrt: {url}" if added else f"War schon gesperrt: {url}", ephemeral=True
        )

    try:
        await client.start(settings.discord_bot_token)
    except Exception:
        log.exception("keyword pinger could not connect")
