"""Pokémon Center queue (waiting room) alarm.

The Pokémon Center store itself sits behind aggressive bot protection, but the
interesting signal for drops is the *virtual queue* (Queue-it): during a drop
the site redirects visitors into the waiting room. This adapter never parses
product pages — it only answers "is the queue live right now?":

    queue live      -> IN_STOCK      (fires BACK_IN_STOCK => Discord ping)
    site normal     -> OUT_OF_STOCK  (idle state)
    bot wall / down -> UNKNOWN       (never treated as a transition source of truth)

Usage: create a watch with the store URL (e.g. https://www.pokemoncenter.com/de-de)
and select adapter "Pokémon Center Queue-Alarm" in the dropdown. It is never
auto-resolved by domain, so ordinary product watches on pokemoncenter.com are
unaffected. Recommended interval 120–300 s, cooldown >= 3600 s.

Detection signals, strongest first:
1. HTTP redirect whose Location points at queue-it.net (redirect mode)
2. final URL on queue-it.net (when a fetcher followed redirects, e.g. Playwright)
3. queue-page phrases in the body — also on HTTP 503, which Queue-it's edge
   mode uses while holding visitors (generic queue-it JS tags on the normal
   page do NOT count, they are present even when no queue runs)

If checks are permanently UNKNOWN, the bot wall is blocking the server's IP:
set PROXY_URL in .env or detection config {"fetcher": "playwright"}.
"""

from __future__ import annotations

import asyncio
import random
import time

import httpx

from app.config import get_settings
from app.models import StockStatus
from app.monitor.base import AdapterError, PageResult, RetailerAdapter, StockResult

QUEUE_HOST_MARKER = "queue-it.net"

QUEUE_PAGE_PHRASES = (
    "you are now in line",
    "you are in line",
    "your place in line",
    "estimated wait",
    "waiting room",
    "virtual queue",
    "du bist jetzt in der warteschlange",
    "deine position in der warteschlange",
    "geschätzte wartezeit",
    "warteraum",
)

REDIRECT_STATUSES = (301, 302, 303, 307, 308)


class PokemonCenterQueueAdapter(RetailerAdapter):
    slug = "pokemon_center_queue"
    name = "Pokémon Center Queue-Alarm"
    domains = ()  # select explicitly via the adapter dropdown, never by domain

    async def fetch(self, url: str) -> PageResult:
        from app.monitor import fetchers

        if self.detection_config.get("fetcher") == "playwright":
            return await fetchers.fetch_playwright(url)
        # NOTE: never route this through the scraping API/unlocker — that is
        # designed to get *past* the anti-bot/queue, which hides the very signal
        # we want. We fetch directly (via PROXY_URL) and DON'T follow redirects,
        # so the Queue-it redirect / waiting-room page is what we detect.
        settings = get_settings()
        await asyncio.sleep(random.uniform(0.2, 1.5))
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(
                headers={
                    "User-Agent": settings.user_agent,
                    "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
                },
                timeout=settings.request_timeout_seconds,
                follow_redirects=False,  # the redirect IS the signal
                proxy=settings.proxy_url,
            ) as client:
                resp = await client.get(url)
        except httpx.HTTPError as exc:
            raise AdapterError(f"pokemon center queue check failed: {exc}") from exc
        return PageResult(
            url=url,
            final_url=str(resp.url),
            status_code=resp.status_code,
            text=resp.text,
            headers={k.lower(): v for k, v in resp.headers.items()},
            elapsed_ms=int((time.monotonic() - start) * 1000),
            fetched_via="httpx",
        )

    def _queue_live(self, note: str) -> StockResult:
        return StockResult(
            status=StockStatus.IN_STOCK,
            title="Pokémon Center Warteschlange",
            alert_title="🚨 Queue ist OFFEN",
            note=note,
        )

    def parse(self, page: PageResult) -> StockResult:
        location = (page.headers.get("location") or "").lower()
        body_lower = page.text[:20000].lower()

        if page.status_code in REDIRECT_STATUSES and QUEUE_HOST_MARKER in location:
            return self._queue_live(f"redirect to queue: {location[:120]}")
        if QUEUE_HOST_MARKER in page.final_url.lower():
            return self._queue_live(f"landed on queue page: {page.final_url[:120]}")
        if any(phrase in body_lower for phrase in QUEUE_PAGE_PHRASES):
            return self._queue_live("queue page content detected")

        if page.status_code == 200 or page.status_code in REDIRECT_STATUSES:
            # site reachable, no queue in sight -> idle
            return StockResult(
                status=StockStatus.OUT_OF_STOCK,
                title="Pokémon Center Warteschlange",
                note="site normal, no queue",
            )
        if page.status_code in (403, 429):
            return StockResult(
                status=StockStatus.UNKNOWN,
                note=f"bot wall (HTTP {page.status_code}) — consider PROXY_URL or playwright",
            )
        return StockResult(status=StockStatus.UNKNOWN, note=f"HTTP {page.status_code}")
