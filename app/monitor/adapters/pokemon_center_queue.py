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
4. early warning: the edge stops answering the way it does at rest (200/403)
   and returns 429 or any 5xx — the wall going up as a drop spins up

Choosing a fetcher (detection config)
-------------------------------------
Measured against pokemoncenter.com, not guessed:

    (default)                  plain httpx  -> permanent HTTP 403 challenge
    {"fetcher": "playwright"}  real browser -> permanent HTTP 403 challenge
    {"fetcher": "brightdata"}  unlocker     -> HTTP 200, full page

Only the unlocker reaches the site, so it is the only setting that can observe
a queue at all. It costs money per request, so keep the interval sane.
"""

from __future__ import annotations

import asyncio
import random
import time

import httpx

from app import proxy as app_proxy
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

# How the edge answers us at rest: the normal store (200) or — far more often —
# the permanent JS challenge Pokémon Center serves to datacenter IPs (403).
# Anything else means the wall moved. 404 is excluded on purpose: that is a
# wrong watch URL, not a drop.
IDLE_STATUSES = (200, 403, 404)


class PokemonCenterQueueAdapter(RetailerAdapter):
    slug = "pokemon_center_queue"
    name = "Pokémon Center Queue-Alarm"
    domains = ()  # select explicitly via the adapter dropdown, never by domain

    async def fetch(self, url: str) -> PageResult:
        from app.monitor import fetchers

        fetcher = self.detection_config.get("fetcher")
        if fetcher == "playwright":
            return await fetchers.fetch_playwright(url)
        if fetcher == "brightdata":
            # The unlocker is the only client that actually reaches this site:
            # plain httpx and a real headless browser both get the permanent
            # 403 challenge wall. It gets past the *bot check* — but Queue-it is
            # a server-side waiting room, not a bot check, so a live queue is
            # served to the unlocker too. We therefore match on the body
            # phrases; the redirect itself is invisible here because the
            # unlocker follows redirects and reports only the requested URL.
            return await fetchers.fetch_scraperapi(url)
        # Direct fetch (via PROXY_URL), NOT following redirects, so a Queue-it
        # redirect is visible as a 3xx + Location. Only useful while the site
        # answers us at all — see the module docstring.
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
                proxy=app_proxy.url(),
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

    def _signal(self, alert_title: str, note: str) -> StockResult:
        return StockResult(
            status=StockStatus.IN_STOCK,
            title="Pokémon Center",
            alert_title=alert_title,
            note=note,
        )

    def parse(self, page: PageResult) -> StockResult:
        location = (page.headers.get("location") or "").lower()
        body_lower = page.text[:20000].lower()
        status = page.status_code

        # 1. Queue-it waiting room reached — the drop is live.
        if (
            (status in REDIRECT_STATUSES and QUEUE_HOST_MARKER in location)
            or QUEUE_HOST_MARKER in page.final_url.lower()
            or any(phrase in body_lower for phrase in QUEUE_PAGE_PHRASES)
        ):
            return self._signal("🚨 Queue ist OFFEN", "queue detected")

        # 2. Heightened anti-bot / overload — PC serves a permanent JS challenge
        #    (403) at rest, so we can't read the queue directly; but when a drop
        #    launches the wall goes up and the edge answers differently. That
        #    change is the "queue coming soon" signal we CAN see. Covers 429
        #    (rate limited) and every 5xx incl. Cloudflare's 52x overload codes.
        if status not in IDLE_STATUSES and (status == 429 or status >= 500):
            label = "Rate-Limit" if status == 429 else "Anti-Bot hoch"
            return self._signal(f"⚠️ PC-Aktivität — {label} (Drop?)", f"HTTP {status}")

        # 3. Steady state: standard JS challenge (403) or the normal store (200)
        #    — idle baseline, no alert (we ping on *changes* away from this).
        return StockResult(
            status=StockStatus.OUT_OF_STOCK,
            title="Pokémon Center",
            note=f"idle (HTTP {status})",
        )
