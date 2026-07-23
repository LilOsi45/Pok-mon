"""Shopify adapter: .json parsing, availability, URL building, registry."""

from __future__ import annotations

import json

from app.models import StockStatus
from app.monitor.adapters.shopify import ShopifyAdapter, product_js_url
from app.monitor.base import PageResult
from app.monitor.registry import resolve_adapter
from tests.conftest import load_fixture


def page(json_data=None, status: int = 200, url="https://cardsrfun.de/products/x") -> PageResult:
    return PageResult(url=url, final_url=url, status_code=status, json_data=json_data)


class TestJsUrl:
    def test_appends_js(self):
        assert (
            product_js_url("https://cardsrfun.de/collections/best-seller/products/op-11")
            == "https://cardsrfun.de/collections/best-seller/products/op-11.js"
        )

    def test_strips_query_and_trailing_slash(self):
        assert (
            product_js_url("https://cardsrfun.de/products/op-11/?variant=42")
            == "https://cardsrfun.de/products/op-11.js"
        )

    def test_converts_json_suffix(self):
        assert product_js_url("https://x.de/products/y.json") == "https://x.de/products/y.js"


class TestParse:
    def test_in_stock_uses_available_variant_price(self):
        data = json.loads(load_fixture("shopify_product.json"))
        result = ShopifyAdapter().parse(page(json_data=data))
        assert result.status == StockStatus.IN_STOCK
        assert result.price == 119.90  # cheapest AVAILABLE variant, not the damaged one
        assert "OP-11" in result.title
        assert result.image_url.endswith("op11.jpg")

    def test_out_of_stock(self):
        data = json.loads(load_fixture("shopify_oos.json"))
        result = ShopifyAdapter().parse(page(json_data=data))
        assert result.status == StockStatus.OUT_OF_STOCK
        assert result.price == 149.90

    def test_js_format_cents_and_toplevel_available(self):
        # Ajax .js: product at top level, prices in cents, `available` flag
        data = {
            "title": "Naruto Display",
            "available": True,
            "featured_image": "https://cardsrfun.de/img/n.jpg",
            "variants": [{"id": 42, "available": True, "price": 7499}],
        }
        result = ShopifyAdapter().parse(page(json_data=data))
        assert result.status == StockStatus.IN_STOCK
        assert result.price == 74.99
        assert result.cart_url == "https://cardsrfun.de/cart/42:1"

    def test_rate_limited_is_unknown(self):
        result = ShopifyAdapter().parse(page(status=429))
        assert result.status == StockStatus.UNKNOWN
        assert "429" in result.note

    def test_non_shopify_json_falls_back(self):
        # HTML page (no product key) -> generic detector, not a crash
        html_page = PageResult(
            url="https://x.de/p",
            final_url="https://x.de/p",
            status_code=200,
            text="<html><body><button>In den Warenkorb</button><span>10,00 €</span></body></html>",
        )
        result = ShopifyAdapter().parse(html_page)
        assert result.status == StockStatus.IN_STOCK  # generic buy-button detection


class TestRegistry:
    def test_shopify_domains_resolve(self):
        assert resolve_adapter("https://cardsrfun.de/products/x").slug == "shopify"
        assert (
            resolve_adapter("https://crispycards.de/collections/one-piece/products/y").slug
            == "shopify"
        )

    def test_selectable_by_slug(self):
        assert (
            resolve_adapter("https://any-shopify.de/products/z", slug="shopify").slug == "shopify"
        )
