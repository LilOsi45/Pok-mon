"""The 🛒 button was missing from most pings.

It is built from the Shopify variant id, and the id only exists in the shop's
JSON. A watch whose handle is not in the shared /products.json — the ordinary
case on a store with more than 250 products, since that is one page — fell
straight through to HTML scraping, which has no variant id and no reliable
availability flag either. One request either way; the JSON one just answers.
"""

from __future__ import annotations

import pytest

from app.models import StockStatus
from app.monitor.adapters.generic import GenericAdapter
from app.monitor.base import PageResult

URL = "https://geeksheaven.de/products/pokemon-30th-etb"

PRODUCT_JSON = {
    "handle": "pokemon-30th-etb",
    "title": "Pokémon 30th Celebration Elite Trainer Box",
    "variants": [{"id": 44556677, "available": True, "price": "59.99"}],
}
HTML = "<html><body><h1>Pokémon 30th ETB</h1><button>In den Warenkorb</button></body></html>"


@pytest.mark.asyncio
class TestCartLinkFromProductJson:
    async def _check(self, monkeypatch, *, verdict: str, json_page: PageResult | None):
        from app.monitor import catalog as shop_catalog

        async def no_catalog_hit(_url):
            return None

        monkeypatch.setattr(shop_catalog, "product_from_catalog", no_catalog_hit)
        monkeypatch.setattr(shop_catalog, "verdict", lambda _url: verdict)
        monkeypatch.setattr("app.monitor.adapters.generic.guard_single_fetch", lambda _url: None)

        adapter = GenericAdapter()

        async def fake_json(_self, _url):
            return json_page

        monkeypatch.setattr(GenericAdapter, "_product_json", fake_json)

        async def fake_html(_url):
            return PageResult(url=URL, final_url=URL, status_code=200, text=HTML)

        monkeypatch.setattr("app.monitor.fetchers.fetch_httpx", lambda url, **kw: fake_html(url))
        return await adapter.check(URL)

    async def test_the_cart_link_is_built_from_the_product_json(self, monkeypatch):
        page = PageResult(url=URL, final_url=URL, status_code=200, text="", json_data=PRODUCT_JSON)
        _page, result = await self._check(monkeypatch, verdict="catalog", json_page=page)

        assert result.status is StockStatus.IN_STOCK
        assert result.cart_url == "https://geeksheaven.de/cart/44556677:1"

    async def test_a_shop_without_a_catalogue_is_not_asked_for_json(self, monkeypatch):
        """Not a Shopify store — the endpoint does not exist, so do not spend a
        request finding that out on every single check."""
        _page, result = await self._check(monkeypatch, verdict="no-catalog", json_page=None)

        assert result.cart_url is None
        assert result.status is StockStatus.IN_STOCK  # HTML buy button

    async def test_html_still_answers_when_the_json_is_not_served(self, monkeypatch):
        _page, result = await self._check(monkeypatch, verdict="catalog", json_page=None)

        assert result.status is StockStatus.IN_STOCK
        assert result.cart_url is None
