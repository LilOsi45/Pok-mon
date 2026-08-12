"""MediaMarkt / Saturn IN-STORE (Marktverfügbarkeit) adapter — EXPERIMENTAL.

Goal: notify when a product is available for **pickup in a store** (as opposed
to online stock, which the regular mediamarkt_de adapter covers).

Reality check: mediamarkt.de / saturn.de are JS-rendered AND behind aggressive
bot protection (Cloudflare + custom). This adapter therefore:
  - forces the Playwright fetcher (a real headless browser),
  - scans the rendered page for German market-availability phrases.

It will almost certainly need a residential PROXY_URL to work from a datacenter
IP — without one, expect UNKNOWN (bot wall). Select it explicitly via the
adapter dropdown; it is never auto-resolved by domain so normal product watches
keep the online-stock adapter.

Signals (Germany-wide "available in some store" heuristic):
  IN_STOCK      -> "in X Märkten verfügbar", "abholbereit", "abholung im markt",
                   "im markt verfügbar", "verfügbar in Ihrer Nähe"
  OUT_OF_STOCK  -> "in keinem markt verfügbar", "nicht im markt verfügbar",
                   "aktuell nicht abholbar"
  UNKNOWN       -> bot wall / challenge / no signal
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from app.models import StockStatus
from app.monitor.base import PageResult, RetailerAdapter, StockResult
from app.monitor.detection import parse_json_ld

IN_STORE_AVAILABLE = (
    "märkten verfügbar",
    "markt verfügbar",
    "abholbereit",
    "abholung im markt",
    "verfügbar in ihrer nähe",
    "click & collect",
    "sofort abholbereit",
)
IN_STORE_UNAVAILABLE = (
    "in keinem markt verfügbar",
    "nicht im markt verfügbar",
    "nicht abholbar",
    "aktuell nicht abholbar",
)
CHALLENGE_MARKERS = ("just a moment", "cf-challenge", "verify you are human", "captcha")


class MediaMarktInStoreAdapter(RetailerAdapter):
    slug = "mediamarkt_instore"
    name = "MediaMarkt Markt-Abholung (experimentell)"
    domains = ()  # explicit selection only
    fetcher = "scraperapi"  # JS + bot protection → route via the unlocker

    def parse(self, page: PageResult) -> StockResult:
        head = page.text[:4000].lower()
        if any(marker in head for marker in CHALLENGE_MARKERS):
            return StockResult(
                status=StockStatus.UNKNOWN,
                note="bot wall/challenge — needs PROXY_URL (residential)",
            )

        tree = HTMLParser(page.text)
        for tag in ("script", "style", "noscript"):
            for node in tree.css(tag):
                node.decompose()
        body_text = (tree.body.text(separator=" ") if tree.body else "").lower()

        title = None
        if node := tree.css_first("h1"):
            title = node.text(strip=True)
        # price is best-effort from JSON-LD (online offer)
        price = None
        if jsonld := parse_json_ld(page.text):
            price = jsonld.price
            title = title or jsonld.title

        note_base = f"instore-scan ({page.fetched_via})"
        if any(p in body_text for p in IN_STORE_UNAVAILABLE):
            return StockResult(
                status=StockStatus.OUT_OF_STOCK,
                price=price,
                title=title,
                note=f"{note_base}: keine Markt-Abholung",
            )
        if any(p in body_text for p in IN_STORE_AVAILABLE):
            return StockResult(
                status=StockStatus.IN_STOCK,
                price=price,
                title=title,
                buy_url=page.final_url,
                alert_title="🏬 Im Markt abholbar",
                note=f"{note_base}: Markt-Abholung verfügbar",
            )
        return StockResult(
            status=StockStatus.UNKNOWN,
            price=price,
            title=title,
            note=f"{note_base}: kein Markt-Signal gefunden (Layout?)",
        )


class SaturnInStoreAdapter(MediaMarktInStoreAdapter):
    slug = "saturn_instore"
    name = "Saturn Markt-Abholung (experimentell)"
