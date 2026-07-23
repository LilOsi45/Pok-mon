"""MediaMarkt.de adapter.

MediaMarkt renders product pages with embedded schema.org JSON-LD; that is the
primary signal. Falls back to German sold-out phrases / add-to-cart heuristics
when JSON-LD is missing. If MediaMarkt's bot protection (Cloudflare) starts
blocking httpx, set {"fetcher": "playwright"} on the watch.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from app.models import StockStatus
from app.monitor.base import PageResult, RetailerAdapter, StockResult
from app.monitor.detection import detect_stock, parse_json_ld, scan_raw_availability


class MediaMarktDeAdapter(RetailerAdapter):
    slug = "mediamarkt_de"
    name = "MediaMarkt.de"
    domains = ("mediamarkt.de", "www.mediamarkt.de")
    # MediaMarkt/Saturn are JS-rendered behind Cloudflare — plain httpx and even
    # Playwright+residential proxy get blocked. Route through the scraping API
    # (no-op fallback to httpx when SCRAPER_API_KEY is unset).
    fetcher = "scraperapi"

    def parse(self, page: PageResult) -> StockResult:
        result = parse_json_ld(page.text)
        if result is not None:
            result.buy_url = result.buy_url or page.final_url
            return result
        # Cloudflare interstitial?
        head = page.text[:3000].lower()
        if "cf-challenge" in head or "just a moment" in head:
            return StockResult(status=StockStatus.UNKNOWN, note="cloudflare challenge")
        tree = HTMLParser(page.text)
        title = None
        if node := tree.css_first("h1"):
            title = node.text(strip=True)
        result = detect_stock(page.text, page_title=title)
        # MediaMarkt embeds schema.org availability in JS/JSON blobs (not clean
        # JSON-LD), so fall back to a raw scan when the heuristics find no signal.
        if result.status == StockStatus.UNKNOWN and (raw := scan_raw_availability(page.text)):
            result.status = raw
            result.note = "schema-raw"
        result.buy_url = result.buy_url or page.final_url
        return result


class SaturnDeAdapter(MediaMarktDeAdapter):
    """Saturn.de runs on the same platform as MediaMarkt — same parsing works."""

    slug = "saturn_de"
    name = "Saturn.de"
    domains = ("saturn.de", "www.saturn.de")


class ExpertDeAdapter(MediaMarktDeAdapter):
    """expert.de — reuse the JSON-LD / schema.org availability cascade. Reports
    online orderable/reservable vs sold out (per-store pickup is not exposed
    Germany-wide, so this tracks purchasability, not a specific Fachmarkt)."""

    slug = "expert_de"
    name = "expert.de"
    domains = ("expert.de", "www.expert.de")
