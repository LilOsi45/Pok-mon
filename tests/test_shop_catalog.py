"""One catalogue request per shop instead of one request per product.

18 watches on geeksheaven.de meant 18 fetches per cycle. With the per-domain
politeness gap that made the configured 120 s interval physically impossible,
so checks queued up instead of running. Serving them from one cached
/products.json fixes both the load and the freshness.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.models import StockStatus
from app.monitor import catalog
from app.monitor.adapters.generic import GenericAdapter
from app.monitor.adapters.shopify import ShopifyAdapter
from app.monitor.base import PageResult

SHOP = "https://geeksheaven.de"

CATALOG = {
    "products": [
        {
            "id": 1,
            "title": "Pokémon 30 Jahre Top-Trainer-Box",
            "handle": "pokemon-30-jahre-ttb",
            "featured_image": "//cdn.shop/ttb.jpg",
            "variants": [
                {"id": 111, "available": False, "price": "59.99"},
            ],
        },
        {
            "id": 2,
            "title": "Pokémon 30 Jahre Booster Bundle",
            "handle": "pokemon-30-jahre-bundle",
            "variants": [
                {"id": 222, "available": True, "price": "25.99"},
                {"id": 223, "available": False, "price": "27.99"},
            ],
        },
    ]
}


def catalog_page(payload: dict | None = None, status: int = 200) -> PageResult:
    body = json.dumps(payload if payload is not None else CATALOG)
    return PageResult(
        url=f"{SHOP}/products.json?limit=250",
        final_url=f"{SHOP}/products.json?limit=250",
        status_code=status,
        text=body,
        json_data=payload if payload is not None else CATALOG,
    )


@pytest.fixture(autouse=True)
def _clear():
    catalog.clear_cache()
    yield
    catalog.clear_cache()


class TestHandle:
    @pytest.mark.parametrize(
        "url,expected",
        [
            (f"{SHOP}/products/pokemon-30-jahre-ttb", "pokemon-30-jahre-ttb"),
            (f"{SHOP}/products/pokemon-30-jahre-ttb.js", "pokemon-30-jahre-ttb"),
            (f"{SHOP}/collections/pokemon/products/abc?variant=9", "abc"),
            (f"{SHOP}/collections/pokemon", None),
            (f"{SHOP}/products/", None),
        ],
    )
    def test_extracts_the_handle(self, url: str, expected: str | None):
        assert catalog.product_handle(url) == expected


@pytest.mark.asyncio
class TestCatalogCache:
    async def test_many_products_share_one_request(self):
        """The whole point: 18 watches must not become 18 fetches."""
        fetch = AsyncMock(return_value=catalog_page())
        with patch("app.monitor.fetchers.fetch_httpx", fetch):
            for _ in range(18):
                await catalog.product_from_catalog(f"{SHOP}/products/pokemon-30-jahre-ttb")
        assert fetch.await_count == 1

    async def test_expired_cache_refetches(self, monkeypatch):
        import app.config as config

        monkeypatch.setenv("CATALOG_TTL_SECONDS", "0")
        config.get_settings.cache_clear()
        fetch = AsyncMock(return_value=catalog_page())
        with patch("app.monitor.fetchers.fetch_httpx", fetch):
            await catalog.product_from_catalog(f"{SHOP}/products/pokemon-30-jahre-ttb")
            await catalog.product_from_catalog(f"{SHOP}/products/pokemon-30-jahre-ttb")
        assert fetch.await_count == 2
        config.get_settings.cache_clear()

    async def test_unknown_handle_returns_none(self):
        with patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=catalog_page())):
            assert await catalog.product_from_catalog(f"{SHOP}/products/gibt-es-nicht") is None

    async def test_non_shopify_shop_is_not_probed_again(self):
        """A shop without the endpoint must not be re-probed on every check."""
        fetch = AsyncMock(return_value=catalog_page({"nope": True}))
        with patch("app.monitor.fetchers.fetch_httpx", fetch):
            for _ in range(5):
                await catalog.product_from_catalog(f"{SHOP}/products/x")
        assert fetch.await_count == 1

    async def test_fetch_error_is_swallowed(self):
        boom = AsyncMock(side_effect=RuntimeError("network down"))
        with patch("app.monitor.fetchers.fetch_httpx", boom):
            assert await catalog.product_from_catalog(f"{SHOP}/products/x") is None


@pytest.mark.asyncio
class TestFailureIsCachedToo:
    """A failed probe must be remembered, not re-run by every waiting watch.

    Guard, not a past bug: the shorter retry window introduced below makes a
    failure expire in two minutes instead of an hour, so it is now much easier
    for 18 watches on one shop to stampede the endpoint the moment it does.
    Both the pre-lock check and the in-lock double-check must accept a cached
    failure as an answer.
    """

    async def test_one_failed_probe_is_not_retried_by_every_watch(self):
        fetch = AsyncMock(return_value=catalog_page({}, status=429))
        with patch("app.monitor.fetchers.fetch_httpx", fetch):
            await asyncio.gather(
                *(catalog.product_from_catalog(f"{SHOP}/products/p{i}") for i in range(18))
            )
        assert fetch.await_count == 1, f"{fetch.await_count} catalogue requests in one burst"

    async def test_a_raising_probe_is_also_cached(self):
        boom = AsyncMock(side_effect=RuntimeError("connection reset"))
        with patch("app.monitor.fetchers.fetch_httpx", boom):
            await asyncio.gather(
                *(catalog.product_from_catalog(f"{SHOP}/products/p{i}") for i in range(10))
            )
        assert boom.await_count == 1


@pytest.mark.asyncio
class TestThrottlingIsNotAVerdict:
    """Regression: a 429 was filed as "this shop has no catalogue" for an hour.

    Every failed probe shared one negative TTL of 3600 s, so a single rate-limit
    answer disabled the catalogue for the whole hour. The 18 per-product fetches
    it was meant to replace kept running, kept provoking 429s, and the hourly
    re-probe landed right back in that flood — the report showed "Katalog 0" on
    every shop while /products.json was working fine when asked politely.
    """

    async def test_rate_limit_is_retried_soon(self):
        assert catalog.RETRY_TTL_SECONDS < catalog.NEGATIVE_TTL_SECONDS / 10

        fetch = AsyncMock(return_value=catalog_page({}, status=429))
        with patch("app.monitor.fetchers.fetch_httpx", fetch):
            await catalog.product_from_catalog(f"{SHOP}/products/x")
        entry = catalog._cache["geeksheaven.de"]
        assert entry.ttl == catalog.RETRY_TTL_SECONDS
        assert "429" in entry.reason

    async def test_a_real_non_shopify_shop_is_shelved_for_an_hour(self):
        fetch = AsyncMock(return_value=catalog_page({"nope": True}))
        with patch("app.monitor.fetchers.fetch_httpx", fetch):
            await catalog.product_from_catalog(f"{SHOP}/products/x")
        assert catalog._cache["geeksheaven.de"].ttl == catalog.NEGATIVE_TTL_SECONDS

    async def test_server_errors_are_transient_too(self):
        fetch = AsyncMock(return_value=catalog_page({}, status=503))
        with patch("app.monitor.fetchers.fetch_httpx", fetch):
            await catalog.product_from_catalog(f"{SHOP}/products/x")
        assert catalog._cache["geeksheaven.de"].ttl == catalog.RETRY_TTL_SECONDS

    async def test_404_is_a_settled_answer(self):
        fetch = AsyncMock(return_value=catalog_page({}, status=404))
        with patch("app.monitor.fetchers.fetch_httpx", fetch):
            await catalog.product_from_catalog(f"{SHOP}/products/x")
        assert catalog._cache["geeksheaven.de"].ttl == catalog.NEGATIVE_TTL_SECONDS

    async def test_the_reason_is_readable(self):
        with patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=catalog_page())):
            await catalog.product_from_catalog(f"{SHOP}/products/pokemon-30-jahre-ttb")
        assert catalog.reason(SHOP) == "2 Produkte"

    async def test_recovery_after_the_retry_window(self, monkeypatch):
        """Once the shop answers again, the catalogue must come back."""
        fetch = AsyncMock(return_value=catalog_page({}, status=429))
        with patch("app.monitor.fetchers.fetch_httpx", fetch):
            assert await catalog.product_from_catalog(f"{SHOP}/products/x") is None

        monkeypatch.setattr(catalog, "RETRY_TTL_SECONDS", 0)
        entry = catalog._cache["geeksheaven.de"]
        catalog._cache["geeksheaven.de"] = catalog._Entry(
            at=entry.at, index=None, reason=entry.reason, ttl=0
        )
        with patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=catalog_page())):
            product = await catalog.product_from_catalog(f"{SHOP}/products/pokemon-30-jahre-ttb")
        assert product is not None


@pytest.mark.asyncio
class TestAdapterIntegration:
    async def test_generic_adapter_reads_stock_from_the_catalog(self):
        """geeksheaven resolves to the generic adapter — it must benefit too."""
        with patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=catalog_page())):
            page, result = await GenericAdapter().check(
                f"{SHOP}/products/pokemon-30-jahre-bundle"
            )
        assert page.fetched_via == "shop-catalog"
        assert result.status == StockStatus.IN_STOCK
        assert result.price == 25.99
        assert result.cart_url == f"{SHOP}/cart/222:1"  # first available variant

    async def test_sold_out_product_is_out_of_stock(self):
        with patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=catalog_page())):
            _page, result = await GenericAdapter().check(f"{SHOP}/products/pokemon-30-jahre-ttb")
        assert result.status == StockStatus.OUT_OF_STOCK
        assert result.cart_url is None

    async def test_non_product_url_uses_the_html_path(self):
        html = PageResult(
            url=f"{SHOP}/collections/x",
            final_url=f"{SHOP}/collections/x",
            status_code=200,
            text="<html><h1>Kategorie</h1></html>",
        )
        with patch.object(GenericAdapter, "fetch", AsyncMock(return_value=html)):
            page, _result = await GenericAdapter().check(f"{SHOP}/collections/x")
        assert page.fetched_via != "shop-catalog"

    async def test_shopify_adapter_falls_back_when_handle_is_missing(self):
        """A catalogue capped at 250 products must not break bigger shops."""
        product_js = PageResult(
            url=f"{SHOP}/products/spaet.js",
            final_url=f"{SHOP}/products/spaet.js",
            status_code=200,
            text=json.dumps({"title": "Spät", "variants": [{"id": 9, "available": True, "price": 1999}]}),
        )

        async def fake_fetch(url, **_kw):
            return catalog_page() if "products.json" in url else product_js

        with patch("app.monitor.fetchers.fetch_httpx", AsyncMock(side_effect=fake_fetch)):
            page = await ShopifyAdapter().fetch(f"{SHOP}/products/spaet")
        assert page.fetched_via != "shop-catalog"
        assert ShopifyAdapter().parse(page).status == StockStatus.IN_STOCK

    async def test_catalog_can_be_switched_off(self, monkeypatch):
        import app.config as config

        monkeypatch.setenv("USE_SHOP_CATALOG", "false")
        config.get_settings.cache_clear()
        html = PageResult(
            url=f"{SHOP}/products/x",
            final_url=f"{SHOP}/products/x",
            status_code=200,
            text="<html><h1>P</h1></html>",
        )
        with patch.object(GenericAdapter, "fetch", AsyncMock(return_value=html)):
            page, _ = await GenericAdapter().check(f"{SHOP}/products/x")
        assert page.fetched_via != "shop-catalog"
        config.get_settings.cache_clear()
