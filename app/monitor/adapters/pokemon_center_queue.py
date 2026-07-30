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
Re-measured against pokemoncenter.com/de-de after the browser handshake and
residential proxies were added — the earlier reading no longer holds:

    (default), server IP       plain httpx  -> HTTP 200
    (default), via proxy       plain httpx  -> HTTP 403 (CloudFront)
    {"fetcher": "brightdata"}  unlocker     -> HTTP 200, full page

So the default is now the right choice: it is free, and only it can see the
Queue-it *redirect*, which the unlocker swallows by following redirects itself.
The proxy must stay out of the way here; unlike the shops, this site blocks the
proxy and answers our own address.
"""

from __future__ import annotations

import asyncio
import random
import time
from urllib.parse import urlsplit

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

# The real store page is ~550 KB. The bot interstitial is about a kilobyte of
# obfuscated JavaScript, served with HTTP 200 and a noindex meta — measured on
# the live server, direct fetch: 1055 characters.
INTERSTITIAL_MAX_CHARS = 8000
STORE_MARKERS = ("/de-de/product", "/en-us/product", "pokémon-sammelkartenspiel", "add to cart")


def _is_interstitial(page: PageResult) -> bool:
    """True when we got a challenge/holding page instead of the store.

    Judged on the body, not the status code: the challenge comes back as 200.
    """
    body = page.text or ""
    if len(body) > INTERSTITIAL_MAX_CHARS:
        return False
    lowered = body.lower()
    return not any(marker in lowered for marker in STORE_MARKERS)


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
            # Paid fallback for when the site stops answering us directly. It
            # gets past a bot check, and Queue-it is a server-side waiting room
            # rather than a bot check, so a live queue should be served to it
            # too — but only the body phrases can show that, because the
            # unlocker follows the redirect itself and reports the requested URL.
            return await fetchers.fetch_scraperapi(url)
        # Direct fetch, NOT following redirects, so a Queue-it redirect is
        # visible as a 3xx + Location — the strongest signal there is, and the
        # one the unlocker hides by following redirects itself.
        #
        # The proxy is only used if the shared routing policy asks for it. This
        # used to pass PROXY_URL unconditionally, which was exactly wrong here:
        # measured against pokemoncenter.com/de-de, the server's own IP gets
        # 200 while the residential proxy gets 403 from CloudFront. The watch
        # would have been blocked on every single check.
        settings = get_settings()
        domain = urlsplit(url).netloc.lower().removeprefix("www.")
        via_proxy = app_proxy.routes_via_proxy(domain, "page")
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
                proxy=app_proxy.url() if via_proxy else None,
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

        # 3. Are we even looking at the store? A bot interstitial is served with
        #    HTTP 200 and about a kilobyte of obfuscated JavaScript, while the
        #    real page is well over half a megabyte. Reading only the status code
        #    made that look like "store normal, no queue" — a watch that can
        #    never see a queue reported as healthy, which is the worst possible
        #    answer for an alarm.
        if _is_interstitial(page):
            return StockResult(
                status=StockStatus.UNKNOWN,
                title="Pokémon Center",
                note=(
                    f"nur Bot-Prüfseite ({len(page.text)} Zeichen, HTTP {status}) — "
                    'blind für die Queue, Detection auf {"fetcher": "brightdata"} setzen'
                ),
            )

        # 4. Steady state: the real store, no queue — idle baseline, no alert
        #    (we ping on *changes* away from this).
        return StockResult(
            status=StockStatus.OUT_OF_STOCK,
            title="Pokémon Center",
            note=f"idle (HTTP {status}, {len(page.text)} Zeichen)",
        )
