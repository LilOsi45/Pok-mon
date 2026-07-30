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
4. a queue-it reference on a page that is not the store — same room, wording we
   do not have; Pokémon Center customises the waiting room text
5. early warning: the edge stops answering the way it does at rest (200/403)
   and returns 429 or any 5xx — the wall going up as a drop spins up
6. last resort: HTTP 200 that is neither the store nor the known challenge. The
   shape of the answer changed, and on this domain that means the waiting room

Signals 4 and 6 exist because signals 1-3 are unproven: nobody here has watched
a live Pokémon Center queue through the unlocker, and the two signals that need
no guesswork (the redirect and the final URL) are both destroyed by the unlocker
following redirects itself. So the adapter also fires on "this is not the page I
know", which needs no knowledge of Queue-it's wording at all.

Choosing a fetcher ("Abruf-Methode" in the watch form)
------------------------------------------------------
Re-measured against pokemoncenter.com/de-de, reading the body size and not just
the status code:

    Standard, server IP        plain httpx  -> 200, but only 1055 chars
                                               (bot interstitial, noindex meta)
    Standard, via proxy        plain httpx  -> 403 (CloudFront)
    Echter Browser             playwright   -> 403, 1565 chars (challenge)
    Unlocker                   scraperapi   -> 200, 547737 chars (the real page)

So the unlocker is the only client that reaches the store, and a queue alarm
without it is blind: `_shape` reports CHALLENGE and the verdict is UNKNOWN,
rather than pretending the store is quiet. Confirmed live afterwards: 615318
characters through the unlocker. The cost is real — one request per interval,
around the clock.

The proxy must stay out of the way either way: unlike the shops, this site
blocks the residential proxy and answers our own address.
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

# The real store page is ~550-615 KB. The bot interstitial is about a kilobyte
# of obfuscated JavaScript, served with HTTP 200 and a noindex meta — measured on
# the live server, direct fetch: 1055 characters, Playwright: 1565.
INTERSTITIAL_MAX_CHARS = 8000
STORE_MARKERS = ("/de-de/product", "/en-us/product", "pokémon-sammelkartenspiel", "add to cart")

# The challenge page's own fingerprint, from both measured samples: a noindex
# meta and a CSS animation that hides the "please enable JavaScript" message.
# Needed to tell a challenge apart from *any other* small page — see _shape.
CHALLENGE_MARKERS = ("#cmsg{animation", "noindex, nofollow")

STORE = "store"
CHALLENGE = "challenge"
OTHER = "other"


def _shape(page: PageResult) -> str:
    """What kind of page came back — judged on the body, never the status code.

    Both the store and the challenge answer with HTTP 200, so the status says
    nothing. Three shapes matter, and the third is the point: a small page that
    is neither the store nor the known challenge is *something new*, and on this
    domain the likeliest something is the waiting room.
    """
    body = page.text or ""
    lowered = body.lower()
    if any(marker in lowered for marker in STORE_MARKERS):
        return STORE
    if len(body) > INTERSTITIAL_MAX_CHARS:
        return STORE  # half a megabyte of markup is the shop, whatever it says
    if any(marker in lowered for marker in CHALLENGE_MARKERS):
        return CHALLENGE
    return OTHER


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
        if fetcher in ("scraperapi", "brightdata"):
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
        shape = _shape(page)

        # 1. Queue-it waiting room reached — the drop is live.
        if (
            (status in REDIRECT_STATUSES and QUEUE_HOST_MARKER in location)
            or QUEUE_HOST_MARKER in page.final_url.lower()
            or any(phrase in body_lower for phrase in QUEUE_PAGE_PHRASES)
        ):
            return self._signal("🚨 Queue ist OFFEN", "queue detected")

        # 1b. Same room, wording we do not have. The phrase list above is in
        #     English and German, but Pokémon Center customises the waiting room
        #     and the unlocker follows the redirect itself, so the URL signal is
        #     gone too. On a page that is NOT the store, a queue-it reference is
        #     the queue. On the store page it means nothing — the tag ships with
        #     every page whether a queue runs or not, which is why the shape
        #     check has to come first.
        if shape is not STORE and QUEUE_HOST_MARKER in body_lower:
            return self._signal("🚨 Queue ist OFFEN", f"queue-it page, HTTP {status}")

        # 2. Heightened anti-bot / overload — PC serves a permanent JS challenge
        #    (403) at rest, so we can't read the queue directly; but when a drop
        #    launches the wall goes up and the edge answers differently. That
        #    change is the "queue coming soon" signal we CAN see. Covers 429
        #    (rate limited) and every 5xx incl. Cloudflare's 52x overload codes.
        if status not in IDLE_STATUSES and (status == 429 or status >= 500):
            label = "Rate-Limit" if status == 429 else "Anti-Bot hoch"
            return self._signal(f"⚠️ PC-Aktivität — {label} (Drop?)", f"HTTP {status}")

        # 3. The store, no queue — idle baseline, no alert (we ping on *changes*
        #    away from this).
        if shape is STORE:
            return StockResult(
                status=StockStatus.OUT_OF_STOCK,
                title="Pokémon Center",
                note=f"idle (HTTP {status}, {len(page.text)} Zeichen)",
            )

        # 4. The bot interstitial: HTTP 200 and about a kilobyte of obfuscated
        #    JavaScript. Reading only the status code made this look like "store
        #    normal, no queue" — a watch that can never see a queue, reported as
        #    healthy, which is the worst possible answer for an alarm.
        if shape is CHALLENGE:
            return StockResult(
                status=StockStatus.UNKNOWN,
                title="Pokémon Center",
                note=(
                    f"nur Bot-Prüfseite ({len(page.text)} Zeichen, HTTP {status}) — blind "
                    'für die Queue, in der Watch "Abruf-Methode: Unlocker" auswählen'
                ),
            )

        # 5. HTTP 200, but neither the store nor a challenge we recognise. The
        #    shop does not serve third page shapes for fun; during a drop it
        #    serves the waiting room. Alarm rather than silence: a false ping
        #    costs one glance, a missed drop costs the drop — and this only fires
        #    when the page changes shape, which at rest it never does.
        if status == 200:
            return self._signal(
                "⚠️ PC-Seite verändert (Queue?)",
                f"unbekannte Seite ({len(page.text)} Zeichen, HTTP 200) — weder Shop noch Bot-Prüfung",
            )

        # 6. Non-200 that IDLE_STATUSES already called normal (403/404): the
        #    permanent challenge or a wrong URL, nothing to announce.
        return StockResult(
            status=StockStatus.UNKNOWN,
            title="Pokémon Center",
            note=f"HTTP {status}, {len(page.text)} Zeichen — kein Urteil möglich",
        )
