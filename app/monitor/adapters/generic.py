"""Generic fallback adapter for any shop without a dedicated integration.

Runs the shared detection pipeline (JSON-LD -> buy button/sold-out phrases ->
price). Good enough for most Shopify / Shopware / JTL shops; write a dedicated
adapter when a shop needs special handling.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from app.monitor.base import PageResult, RetailerAdapter, StockResult
from app.monitor.detection import detect_stock


def guard_single_fetch(url: str) -> None:
    """Refuse a per-product request while the shop's catalogue is merely unreachable.

    Measured on geeksheaven.de: 18 watches falling back to one request each is
    what tripped Cloudflare's rate limit, and the limit then blocked the very
    catalogue request that would have replaced all 18. Five minutes of complete
    silence cleared it, so the way out is to send nothing until the catalogue
    answers again — not to keep asking eighteen times a round.

    A shop that has been *confirmed* not to publish a catalogue is unaffected;
    single fetches are the only option there.
    """
    from app.monitor.catalog import CatalogUnavailable, product_handle, reason, verdict

    if product_handle(url) is None:
        return  # not a product URL, so the catalogue was never going to answer it
    if verdict(url) == "unavailable":
        raise CatalogUnavailable(
            f"Katalog nicht erreichbar ({reason(url) or 'noch nicht geprüft'}) — "
            "kein Einzelabruf, damit die Sperre ablaufen kann"
        )


class GenericAdapter(RetailerAdapter):
    slug = "generic"
    name = "Generic shop"
    domains = ()

    async def check(self, url: str) -> tuple[PageResult, StockResult]:
        """Serve Shopify products from the shop's shared catalogue when possible.

        Auto-detected per shop, so any Shopify store benefits without being
        listed anywhere: 18 watches on one shop become 1 request instead of 18,
        and the reading is the authoritative `available` flag rather than HTML
        guesswork. Non-Shopify shops fall through to the normal HTML path.
        """
        from app.config import get_settings

        if get_settings().use_shop_catalog and "/products/" in url:
            from app.monitor import catalog as shop_catalog
            from app.monitor.adapters.shopify import ShopifyAdapter

            product = await shop_catalog.product_from_catalog(url)
            if product is not None:
                page = PageResult(
                    url=url,
                    final_url=url,
                    status_code=200,
                    text="",
                    json_data=product,
                    fetched_via="shop-catalog",
                )
                return page, ShopifyAdapter(self.detection_config).parse(page)
            guard_single_fetch(url)
        return await super().check(url)

    def parse(self, page: PageResult) -> StockResult:
        title = None
        tree = HTMLParser(page.text)
        if node := tree.css_first("h1"):
            title = node.text(strip=True)
        result = detect_stock(page.text, page_title=title)
        result.buy_url = result.buy_url or page.final_url
        return result
