"""Discord webhook notifier with rich, color-coded embeds."""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

from app.config import get_settings
from app.events import Event
from app.models import EventType, Game
from app.notify.base import Notifier, NotifierNotConfigured, NotifyError
from app.shops import other_shop_links

log = logging.getLogger(__name__)

# Holo Foil theme colors (decimal RGB for Discord)
COLOR_VIOLET = 0x8B5CF6
COLOR_CYAN = 0x2DD4BF
COLOR_MAGENTA = 0xEC4899  # reserved for restock/available alerts

EVENT_COLORS: dict[EventType, int] = {
    EventType.BACK_IN_STOCK: COLOR_MAGENTA,
    EventType.NEW_LISTING: COLOR_MAGENTA,
    EventType.PRICE_DROP: COLOR_CYAN,
    EventType.NEW_SET_ANNOUNCED: COLOR_VIOLET,
    EventType.PREORDER_LIVE: COLOR_CYAN,
    EventType.RELEASE_SOON: COLOR_CYAN,
    EventType.NEW_SHOP_FOUND: COLOR_CYAN,
    EventType.HEARTBEAT: COLOR_VIOLET,
    EventType.TEST: COLOR_VIOLET,
}

EVENT_EMOJI: dict[EventType, str] = {
    EventType.BACK_IN_STOCK: "🟢",
    EventType.NEW_LISTING: "✨",
    EventType.PRICE_DROP: "💰",
    EventType.NEW_SET_ANNOUNCED: "📣",
    EventType.PREORDER_LIVE: "🛒",
    EventType.RELEASE_SOON: "⏰",
    EventType.NEW_SHOP_FOUND: "🔎",
    EventType.HEARTBEAT: "✅",
    EventType.TEST: "🧪",
}

GAME_LABEL = {Game.POKEMON: "Pokémon TCG", Game.ONE_PIECE: "One Piece Card Game"}


def build_embed(event: Event) -> dict:
    emoji = EVENT_EMOJI.get(event.type, "🔔")
    embed: dict = {
        "title": f"{emoji} {event.title}"[:256],
        "color": EVENT_COLORS.get(event.type, COLOR_VIOLET),
        "timestamp": event.created_at.isoformat(),
        "footer": {"text": f"TCG Tracker · {event.type.value}"},
        "fields": [],
    }
    if event.message:
        embed["description"] = event.message[:4000]
    if event.url:
        embed["url"] = event.url
    if event.image_url:
        embed["thumbnail"] = {"url": event.image_url}
    if event.game:
        embed["fields"].append(
            {"name": "Game", "value": GAME_LABEL.get(event.game, event.game.value), "inline": True}
        )
    if event.retailer:
        embed["fields"].append({"name": "Retailer", "value": event.retailer, "inline": True})
    if event.price is not None:
        currency = event.currency or "EUR"
        symbol = "€" if currency.upper() == "EUR" else currency
        embed["fields"].append(
            {"name": "Price", "value": f"{event.price:.2f} {symbol}", "inline": True}
        )
    if event.url and event.is_stock_event:
        embed["fields"].append({"name": "Buy", "value": f"[Open product page]({event.url})"})
    if event.is_stock_event and get_settings().show_other_shops:
        # product search across the other big (Trustpilot-vetted) shops
        query = event.message or event.title.split(": ", 1)[-1]
        parts: list[str] = []
        for name, url in other_shop_links(query, event.game, exclude_url=event.url):
            link = f"[{name}]({url})"
            if sum(len(p) + 3 for p in parts) + len(link) > 1000:  # field limit is 1024
                break
            parts.append(link)
        if parts:
            embed["fields"].append({"name": "Auch checken", "value": " · ".join(parts)})
    return embed


def build_payload(event: Event) -> dict:
    payload: dict = {"username": "TCG Tracker", "embeds": [build_embed(event)]}
    if event.priority:  # real phone push with sound, hard to miss
        payload["content"] = "@everyone"
        payload["allowed_mentions"] = {"parse": ["everyone"]}
    return payload


class DiscordNotifier(Notifier):
    type = "discord"

    def _webhook_url(self) -> str:
        url = self.config.get("webhook_url")
        if not url and (env_key := self.config.get("webhook_url_env")):
            url = os.environ.get(env_key)
        if not url:
            raise NotifierNotConfigured(
                f"discord notifier '{self.name}': no webhook_url / webhook_url_env configured"
            )
        return url

    async def send(self, event: Event) -> None:
        payload = build_payload(event)
        url = self._webhook_url()
        async with httpx.AsyncClient(timeout=15) as client:
            for attempt in range(3):
                try:
                    resp = await client.post(url, json=payload)
                except httpx.HTTPError as exc:
                    if attempt == 2:
                        raise NotifyError(f"discord request failed: {exc}") from exc
                    await asyncio.sleep(2**attempt)
                    continue
                if resp.status_code in (200, 204):
                    return
                if resp.status_code == 429:  # rate limited — honor Retry-After
                    retry_after = float(resp.headers.get("Retry-After", "2"))
                    log.warning(
                        "discord rate limit on '%s', retrying in %.1fs", self.name, retry_after
                    )
                    await asyncio.sleep(min(retry_after, 30))
                    continue
                raise NotifyError(
                    f"discord webhook returned HTTP {resp.status_code}: {resp.text[:200]}"
                )
        raise NotifyError("discord webhook: retries exhausted (rate limited)")
