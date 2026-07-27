"""While a shop's catalogue is unreachable, do not fetch products one by one.

Proven on the live server: geeksheaven.de answered 429 to everything, but after
five minutes of complete silence a single request got a clean 200. The ban was
short — we just never stopped talking. What kept it topped up was the fallback:
whenever the catalogue request failed, all 18 watches on that shop went out and
fetched their product page individually, which is both the load that trips
Cloudflare and the reason the replacement request could never get through.

Falling back is still right when a shop is *confirmed* to have no catalogue —
there single fetches are the only option.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.models import StockStatus
from app.monitor import catalog
from app.monitor.adapters.generic import GenericAdapter
from app.monitor.adapters.shopify import ShopifyAdapter
from app.monitor.base import PageResult

SHOP = "https://geeksheaven.de"
PRODUCT = f"{SHOP}/products/pokemon-30-jahre-ttb"

CATALOG = {
    "products": [
        {
            "id": 1,
            "title": "Pokémon 30 Jahre Top-Trainer-Box",
            "handle": "pokemon-30-jahre-ttb",
            "variants": [{"id": 111, "available": True, "price": "59.99"}],
        }
    ]
}


@pytest.fixture(autouse=True)
def _clear():
    catalog.clear_cache()
    yield
    catalog.clear_cache()


def _page(payload: dict, status: int = 200) -> PageResult:
    return PageResult(
        url=f"{SHOP}/products.json?limit=250",
        final_url=f"{SHOP}/products.json?limit=250",
        status_code=status,
        text=json.dumps(payload),
        json_data=payload,
    )


class TestVerdict:
    def test_unprobed_shop_is_unknown(self):
        assert catalog.verdict(PRODUCT) == "unavailable"

    @pytest.mark.asyncio
    async def test_a_rate_limited_shop_stays_unknown(self):
        with patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=_page({}, 429))):
            await catalog.product_from_catalog(PRODUCT)
        assert catalog.verdict(PRODUCT) == "unavailable"

    @pytest.mark.asyncio
    async def test_a_confirmed_non_shopify_shop_is_settled(self):
        with patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=_page({"no": 1}))):
            await catalog.product_from_catalog(PRODUCT)
        assert catalog.verdict(PRODUCT) == "no-catalog"

    @pytest.mark.asyncio
    async def test_a_working_catalog_is_settled(self):
        with patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=_page(CATALOG))):
            await catalog.product_from_catalog(PRODUCT)
        assert catalog.verdict(PRODUCT) == "catalog"


@pytest.mark.asyncio
class TestGenericAdapter:
    async def test_no_single_fetch_while_the_shop_refuses_us(self):
        """The regression: this fallback is what kept the ban alive."""
        probe = AsyncMock(return_value=_page({}, 429))
        single = AsyncMock()
        with (
            patch("app.monitor.fetchers.fetch_httpx", probe),
            patch.object(GenericAdapter, "fetch", single),
            pytest.raises(catalog.CatalogUnavailable),
        ):
            await GenericAdapter().check(PRODUCT)

        single.assert_not_awaited()

    async def test_the_error_explains_itself(self):
        with (
            patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=_page({}, 429))),
            pytest.raises(catalog.CatalogUnavailable, match="Katalog nicht erreichbar"),
        ):
            await GenericAdapter().check(PRODUCT)

    async def test_a_shop_without_a_catalog_is_still_fetched_singly(self):
        """Not every shop is Shopify — those must keep working."""
        html = PageResult(
            url=PRODUCT, final_url=PRODUCT, status_code=200, text="<h1>Box</h1>In den Warenkorb"
        )
        with (
            patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=_page({"no": 1}))),
            patch.object(GenericAdapter, "fetch", AsyncMock(return_value=html)) as single,
        ):
            page, _result = await GenericAdapter().check(PRODUCT)

        single.assert_awaited_once()
        assert page.fetched_via != "shop-catalog"

    async def test_the_catalog_path_still_works(self):
        with patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=_page(CATALOG))):
            page, result = await GenericAdapter().check(PRODUCT)
        assert page.fetched_via == "shop-catalog"
        assert result.status == StockStatus.IN_STOCK

    async def test_a_product_missing_from_a_working_catalog_may_be_fetched(self):
        """A store with more than 250 products must not lose its extra watches."""
        html = PageResult(
            url=f"{SHOP}/products/spaet",
            final_url=f"{SHOP}/products/spaet",
            status_code=200,
            text="<h1>Spät</h1>ausverkauft",
        )
        with (
            patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=_page(CATALOG))),
            patch.object(GenericAdapter, "fetch", AsyncMock(return_value=html)) as single,
        ):
            await GenericAdapter().check(f"{SHOP}/products/spaet")

        single.assert_awaited_once()

    async def test_a_collection_url_is_untouched(self):
        html = PageResult(
            url=f"{SHOP}/collections/neu",
            final_url=f"{SHOP}/collections/neu",
            status_code=200,
            text="<h1>Neu</h1>",
        )
        with patch.object(GenericAdapter, "fetch", AsyncMock(return_value=html)) as single:
            await GenericAdapter().check(f"{SHOP}/collections/neu")
        single.assert_awaited_once()

    async def test_a_products_url_without_a_handle_is_not_blocked(self):
        """/products/ with nothing after it was never a catalogue lookup."""
        html = PageResult(
            url=f"{SHOP}/products/",
            final_url=f"{SHOP}/products/",
            status_code=200,
            text="<h1>x</h1>",
        )
        with patch.object(GenericAdapter, "fetch", AsyncMock(return_value=html)) as single:
            await GenericAdapter().check(f"{SHOP}/products/")
        single.assert_awaited_once()


@pytest.mark.asyncio
class TestShopifyAdapter:
    async def test_it_refuses_the_js_endpoint_while_the_shop_refuses_us(self):
        fetch = AsyncMock(return_value=_page({}, 429))
        with (
            patch("app.monitor.fetchers.fetch_httpx", fetch),
            pytest.raises(catalog.CatalogUnavailable),
        ):
            await ShopifyAdapter().fetch(PRODUCT)

        assert fetch.await_count == 1, "only the catalogue probe may go out"
