"""Shopify storefront adapter.

Most modern TCG shops run on Shopify, which exposes a clean, lightweight
product JSON at `<product-url>.json` (e.g.
https://cardsrfun.de/products/<handle>.json). That endpoint returns structured
variant data — availability + price — far more reliably than scraping the
JS-rendered HTML, and it is much cheaper for the shop to serve (fewer 429s).

Availability: a product is IN_STOCK when any variant has `available: true`.
Falls back to the generic HTML detector if the JSON endpoint is unavailable.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit, urlunsplit

from app.models import StockStatus
from app.monitor.adapters.generic import GenericAdapter
from app.monitor.base import PageResult, RetailerAdapter, StockResult
from app.monitor.detection import parse_german_price

log = logging.getLogger(__name__)


def product_json_url(url: str) -> str:
    """Turn a Shopify product URL into its `.json` endpoint (query stripped)."""
    split = urlsplit(url)
    path = split.path.rstrip("/")
    if not path.endswith(".json"):
        path = f"{path}.json"
    return urlunsplit((split.scheme, split.netloc, path, "", ""))


class ShopifyAdapter(RetailerAdapter):
    slug = "shopify"
    name = "Shopify shop"
    # Confirmed Shopify TCG shops (product URLs look like /products/<handle>).
    domains = ("cardsrfun.de", "crispycards.de", "cardcosmos.de")

    async def fetch(self, url: str) -> PageResult:
        from app.monitor import fetchers

        json_url = product_json_url(url)
        page = await fetchers.fetch_httpx(json_url, respect_robots=False)
        # Keep the human URL as the buy link
        page.url = url
        return page

    def parse(self, page: PageResult) -> StockResult:
        if page.status_code == 404:
            return StockResult(status=StockStatus.UNKNOWN, listed=False, note="product not found")
        if page.status_code == 429:
            return StockResult(
                status=StockStatus.UNKNOWN,
                note="shop rate-limited (429) — increase interval / fewer watches per shop",
            )
        product = page.json_data.get("product") if isinstance(page.json_data, dict) else None
        if not product:
            # Not JSON (maybe not Shopify after all) — fall back to HTML detection
            return GenericAdapter(self.detection_config).parse(page)

        variants = product.get("variants") or []
        available = any(v.get("available") for v in variants)

        # One-tap add-to-cart permalink for the first available variant.
        cart_url = None
        first_available = next((v for v in variants if v.get("available") and v.get("id")), None)
        if first_available:
            split = urlsplit(page.url)
            cart_url = f"{split.scheme}://{split.netloc}/cart/{first_available['id']}:1"
        prices = [
            parse_german_price(v.get("price"))
            for v in variants
            if v.get("available") and v.get("price") is not None
        ]
        if not prices:
            prices = [parse_german_price(v.get("price")) for v in variants if v.get("price")]
        price = min((p for p in prices if p is not None), default=None)

        image = None
        if isinstance(product.get("image"), dict):
            image = product["image"].get("src")
        elif product.get("images"):
            first = product["images"][0]
            image = first.get("src") if isinstance(first, dict) else None

        buy_url = page.url
        return StockResult(
            status=StockStatus.IN_STOCK if available else StockStatus.OUT_OF_STOCK,
            price=price,
            currency="EUR",
            title=product.get("title"),
            image_url=image,
            buy_url=buy_url,
            cart_url=cart_url,
            note=f"shopify: {sum(1 for v in variants if v.get('available'))}/{len(variants)} variants available",
        )
