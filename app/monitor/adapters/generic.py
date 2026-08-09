"""Generic fallback adapter for any shop without a dedicated integration.

Runs the shared detection pipeline (JSON-LD -> buy button/sold-out phrases ->
price). Good enough for most Shopify / Shopware / JTL shops; write a dedicated
adapter when a shop needs special handling.
"""

from __future__ import annotations

import asyncio
import logging

from selectolax.parser import HTMLParser

from app.monitor.base import PageResult, RetailerAdapter, StockResult
from app.monitor.detection import detect_stock

log = logging.getLogger(__name__)

# Long enough that a shop trimming its response under load has moved on, short
# enough to stay well inside the poll interval.
RETRY_UNREADABLE_SECONDS = 4.0


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
            # The handle is missing from a catalogue the shop *does* publish —
            # the ordinary case is a store with more than 250 products, since
            # that is one page of /products.json. Falling straight to HTML here
            # threw away everything the JSON would have given: the authoritative
            # `available` flag and the variant id, and without that id there is
            # no add-to-cart link. That is why the 🛒 button was missing from so
            # many pings. The single-product endpoint costs the same one request
            # as the HTML page it replaces.
            if shop_catalog.verdict(url) == "catalog":
                page = await self._product_json(url)
                if page is not None:
                    return page, ShopifyAdapter(self.detection_config).parse(page)

        page, result = await super().check(url)
        if result.readable or page.status_code == 404:
            return page, result
        # Nothing on the page said anything about stock. Measured on
        # elbenwald.de: the same URL served 533415 characters with a buy button
        # and a price on one fetch and 40459 characters with none of it on the
        # next, and 89 % of that watch's checks landed on the short version.
        # One more attempt costs a single request, and only on a check that
        # would otherwise have been thrown away.
        log.info("no stock signal on %s — one more attempt", url)
        await asyncio.sleep(RETRY_UNREADABLE_SECONDS)
        try:
            retry_page, retry_result = await super().check(url)
        except Exception as exc:
            log.info("retry for %s failed (%s) — keeping the first reading", url, exc)
            return page, result
        return (retry_page, retry_result) if retry_result.readable else (page, result)

    async def _product_json(self, url: str) -> PageResult | None:
        """The shop's own JSON for one product, or None if it isn't served."""
        from app.monitor import fetchers
        from app.monitor.adapters.shopify import product_js_url

        try:
            page = await fetchers.fetch_httpx(product_js_url(url), respect_robots=False)
        except Exception as exc:
            # Not fatal: the HTML path below still works, just with less data.
            log.info("product JSON unavailable for %s (%s) — falling back to HTML", url, exc)
            return None
        if page.status_code != 200 or not (page.text or page.json_data):
            return None
        page.url = url  # keep the human URL as the buy link
        return page

    def parse(self, page: PageResult) -> StockResult:
        title = None
        tree = HTMLParser(page.text)
        if node := tree.css_first("h1"):
            title = node.text(strip=True)
        result = detect_stock(page.text, page_title=title)
        result.buy_url = result.buy_url or page.final_url
        return result
