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
from app.monitor.detection import detect_stock, parse_json_ld


class MediaMarktDeAdapter(RetailerAdapter):
    slug = "mediamarkt_de"
    name = "MediaMarkt.de"
    domains = ("mediamarkt.de", "www.mediamarkt.de")

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
        result.buy_url = result.buy_url or page.final_url
        return result


class SaturnDeAdapter(MediaMarktDeAdapter):
    """Saturn.de runs on the same platform as MediaMarkt — same parsing works."""

    slug = "saturn_de"
    name = "Saturn.de"
    domains = ("saturn.de", "www.saturn.de")
