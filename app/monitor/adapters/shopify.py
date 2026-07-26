"""Shopify storefront adapter.

Most modern TCG shops run on Shopify, which exposes lightweight product JSON.
We use the Ajax endpoint `<product-url>.js` because it reliably includes the
computed `available` flag per variant (the legacy `<product-url>.json` omits it
on many stores, e.g. cardsrfun.de). Prices on `.js` are integer cents.

Availability: a product is IN_STOCK when the product (or any variant) is
`available`. Falls back to the generic HTML detector if the endpoint is
unavailable.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit, urlunsplit

from app.models import StockStatus
from app.monitor.adapters.generic import GenericAdapter
from app.monitor.base import PageResult, RetailerAdapter, StockResult
from app.monitor.detection import parse_german_price

log = logging.getLogger(__name__)


def product_js_url(url: str) -> str:
    """Turn a Shopify product URL into its `.js` Ajax endpoint (query stripped)."""
    split = urlsplit(url)
    path = split.path.rstrip("/")
    for suffix in (".js", ".json"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    return urlunsplit((split.scheme, split.netloc, f"{path}.js", "", ""))


def _variant_price(variant: dict) -> float | None:
    """`.js` prices are integer cents; `.json` prices are decimal strings."""
    price = variant.get("price")
    if isinstance(price, bool) or price is None:
        return None
    if isinstance(price, int):
        return round(price / 100, 2)
    return parse_german_price(price)


def _product_image(product: dict) -> str | None:
    image = None
    if isinstance(product.get("featured_image"), str):  # .js
        image = product["featured_image"]
    elif isinstance(product.get("image"), dict):  # .json
        image = product["image"].get("src")
    elif isinstance(product.get("image"), str):
        image = product["image"]
    elif product.get("images"):
        first = product["images"][0]
        image = first if isinstance(first, str) else (first.get("src") if isinstance(first, dict) else None)
    if image and image.startswith("//"):  # Shopify returns protocol-relative URLs
        image = f"https:{image}"
    return image


class ShopifyAdapter(RetailerAdapter):
    slug = "shopify"
    name = "Shopify shop"
    # Confirmed Shopify TCG shops (product URLs look like /products/<handle>).
    domains = ("cardsrfun.de", "crispycards.de", "cardcosmos.de")

    async def fetch(self, url: str) -> PageResult:
        from app.config import get_settings
        from app.monitor import fetchers
        from app.monitor.catalog import product_from_catalog

        # One cached /products.json per shop covers every watch on it; only fall
        # back to a per-product request when the handle isn't in the catalogue
        # (e.g. a store with more than 250 products).
        if get_settings().use_shop_catalog:
            product = await product_from_catalog(url)
            if product is not None:
                return PageResult(
                    url=url,
                    final_url=url,
                    status_code=200,
                    text="",
                    json_data=product,
                    fetched_via="shop-catalog",
                )
        page = await fetchers.fetch_httpx(product_js_url(url), respect_robots=False)
        page.url = url  # keep the human URL as the buy link
        return page

    def parse(self, page: PageResult) -> StockResult:
        if page.status_code == 404:
            return StockResult(status=StockStatus.UNKNOWN, listed=False, note="product not found")
        if page.status_code == 429:
            return StockResult(
                status=StockStatus.UNKNOWN,
                note="shop rate-limited (429) — increase interval / fewer watches per shop",
            )
        data = page.json_data if isinstance(page.json_data, dict) else None
        # `.js` is served as application/javascript, so the fetcher doesn't
        # auto-parse it — decode the body ourselves (it is valid JSON).
        if data is None and page.text:
            try:
                data = json.loads(page.text)
            except ValueError:
                data = None
        # `.js` returns the product at the top level; `.json` wraps it in "product".
        product = data.get("product") if data and isinstance(data.get("product"), dict) else data
        if not isinstance(product, dict) or not product.get("variants"):
            # Not JSON (maybe not Shopify after all) — fall back to HTML detection
            return GenericAdapter(self.detection_config).parse(page)

        variants = product.get("variants") or []
        available = product.get("available")
        if available is None:
            available = any(v.get("available") for v in variants)

        # One-tap add-to-cart permalink for the first available variant.
        cart_url = None
        first_available = next((v for v in variants if v.get("available") and v.get("id")), None)
        if first_available:
            split = urlsplit(page.url)
            cart_url = f"{split.scheme}://{split.netloc}/cart/{first_available['id']}:1"

        prices = [p for v in variants if v.get("available") if (p := _variant_price(v)) is not None]
        if not prices:
            prices = [p for v in variants if (p := _variant_price(v)) is not None]
        price = min(prices, default=None)

        buy_url = page.url
        return StockResult(
            status=StockStatus.IN_STOCK if available else StockStatus.OUT_OF_STOCK,
            price=price,
            currency="EUR",
            title=product.get("title"),
            image_url=_product_image(product),
            buy_url=buy_url,
            cart_url=cart_url,
            note=f"shopify: {sum(1 for v in variants if v.get('available'))}/{len(variants)} variants available",
        )
