"""games-island.eu — dedicated German TCG shop (JTL-Shop platform).

Product pages ship schema.org JSON-LD with offer availability (primary
signal); the fallback checks the JTL availability badge / add-to-cart form.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from app.models import StockStatus
from app.monitor.base import PageResult, RetailerAdapter, StockResult
from app.monitor.detection import detect_stock, parse_german_price, parse_json_ld

AVAILABLE_BADGES = ("sofort verfügbar", "auf lager", "lieferbar", "knapper lagerbestand")
UNAVAILABLE_BADGES = ("ausverkauft", "nicht auf lager", "momentan nicht verfügbar")


class GamesIslandAdapter(RetailerAdapter):
    slug = "games_island"
    name = "Games Island"
    domains = ("games-island.eu", "www.games-island.eu")

    def parse(self, page: PageResult) -> StockResult:
        result = parse_json_ld(page.text)
        if result is not None:
            result.buy_url = result.buy_url or page.final_url
            return result

        tree = HTMLParser(page.text)
        title = None
        if node := tree.css_first("h1.product-title, h1[itemprop='name'], h1"):
            title = node.text(strip=True)
        badge_text = ""
        if node := tree.css_first(
            ".status, .delivery-status, .availability, [class*='verfuegbar']"
        ):
            badge_text = node.text(deep=True, strip=True).lower()
        price = None
        if node := tree.css_first("[itemprop='price']"):
            price = parse_german_price(
                (node.attributes or {}).get("content") or node.text(strip=True)
            )

        if any(b in badge_text for b in UNAVAILABLE_BADGES):
            return StockResult(
                status=StockStatus.OUT_OF_STOCK,
                price=price,
                title=title,
                note=f"badge: {badge_text[:60]}",
            )
        if any(b in badge_text for b in AVAILABLE_BADGES):
            return StockResult(
                status=StockStatus.IN_STOCK,
                price=price,
                title=title,
                buy_url=page.final_url,
                note=f"badge: {badge_text[:60]}",
            )
        result = detect_stock(page.text, page_title=title)
        result.buy_url = result.buy_url or page.final_url
        return result
