"""Generic fallback adapter for any shop without a dedicated integration.

Runs the shared detection pipeline (JSON-LD -> buy button/sold-out phrases ->
price). Good enough for most Shopify / Shopware / JTL shops; write a dedicated
adapter when a shop needs special handling.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from app.monitor.base import PageResult, RetailerAdapter, StockResult
from app.monitor.detection import detect_stock


class GenericAdapter(RetailerAdapter):
    slug = "generic"
    name = "Generic shop"
    domains = ()

    def parse(self, page: PageResult) -> StockResult:
        title = None
        tree = HTMLParser(page.text)
        if node := tree.css_first("h1"):
            title = node.text(strip=True)
        result = detect_stock(page.text, page_title=title)
        result.buy_url = result.buy_url or page.final_url
        return result
