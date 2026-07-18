"""Amazon.de product-page adapter.

Notes:
- Amazon aggressively rate-limits scrapers; keep intervals >= 5 min and expect
  occasional captcha interstitials (reported as UNKNOWN with a note, never as
  a stock transition).
- Switch a watch to the Playwright fetcher via detection config
  {"fetcher": "playwright"} if httpx keeps hitting the bot wall.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from app.models import StockStatus
from app.monitor.base import PageResult, RetailerAdapter, StockResult
from app.monitor.detection import parse_german_price

UNAVAILABLE_PHRASES = (
    "derzeit nicht verfügbar",
    "currently unavailable",
    "derzeit nicht auf lager",
)
IN_STOCK_PHRASES = (
    "auf lager",
    "in stock",
    "jetzt vorbestellen",
    "vorbestellbar",
)
CAPTCHA_MARKERS = (
    "api-services-support@amazon.com",
    "geben sie die zeichen unten ein",
    "type the characters you see in this image",
)


class AmazonDeAdapter(RetailerAdapter):
    slug = "amazon_de"
    name = "Amazon.de"
    domains = ("amazon.de", "www.amazon.de")
    extra_headers = {"Accept-Language": "de-DE,de;q=0.9"}

    def parse(self, page: PageResult) -> StockResult:
        html_lower_head = page.text[:4000].lower()
        if any(marker in page.text.lower()[:6000] for marker in CAPTCHA_MARKERS) or (
            "captcha" in html_lower_head and "amazon" in html_lower_head
        ):
            return StockResult(status=StockStatus.UNKNOWN, note="amazon captcha/bot wall")

        tree = HTMLParser(page.text)
        title_node = tree.css_first("#productTitle")
        title = title_node.text(strip=True) if title_node else None

        image = None
        if img := tree.css_first("#landingImage"):
            image = (img.attributes or {}).get("src")

        price = None
        for selector in (".a-price .a-offscreen", "#corePrice_feature_div .a-offscreen"):
            if node := tree.css_first(selector):
                price = parse_german_price(node.text(strip=True))
                if price is not None:
                    break

        availability_text = ""
        if node := tree.css_first("#availability"):
            availability_text = node.text(deep=True, strip=True).lower()

        has_cart = tree.css_first("#add-to-cart-button") is not None
        has_buy_now = tree.css_first("#buy-now-button") is not None

        if any(p in availability_text for p in UNAVAILABLE_PHRASES):
            return StockResult(
                status=StockStatus.OUT_OF_STOCK,
                price=price,
                title=title,
                image_url=image,
                note=f"availability: {availability_text[:80]}",
            )
        if has_cart or has_buy_now:
            return StockResult(
                status=StockStatus.IN_STOCK,
                price=price,
                title=title,
                image_url=image,
                buy_url=page.final_url,
                note="add-to-cart present",
            )
        if any(p in availability_text for p in IN_STOCK_PHRASES):
            return StockResult(
                status=StockStatus.IN_STOCK,
                price=price,
                title=title,
                image_url=image,
                buy_url=page.final_url,
                note=f"availability: {availability_text[:80]}",
            )
        if title is None:
            # Not a product page we understand (blocked, redirected, layout change)
            return StockResult(status=StockStatus.UNKNOWN, note="no product title found")
        return StockResult(
            status=StockStatus.OUT_OF_STOCK,
            price=price,
            title=title,
            image_url=image,
            note="no buy box",
        )
