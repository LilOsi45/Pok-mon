"""/products.json has its own budget — meter it separately from ordinary pages.

Measured on the live server: the health report showed the catalogue being
refused over and over, and the pause stuck at exactly 60s every single time,
never doubling. Meanwhile the scanner's collection pages on the same shop kept
answering 200. One shared cooldown made both facts harmful:

  * every successful page reset the catalogue's penalty to zero, so the wait
    never grew and /products.json never got the quiet stretch it needs — five
    minutes of silence had been shown to turn a solid 429 into a 200;
  * a catalogue refusal took the working pages offline as well, so scanners
    failed for a limit that never applied to them.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.monitor import catalog, fetchers

SHOP = "geeksheaven.de"
CATALOG_URL = f"https://{SHOP}/products.json?limit=250"
PAGE_URL = f"https://{SHOP}/collections/neuheiten"


@pytest.fixture(autouse=True)
def _clean():
    fetchers.reset_cooldowns()
    catalog.clear_cache()
    yield
    fetchers.reset_cooldowns()
    catalog.clear_cache()


class TestBucketOf:
    @pytest.mark.parametrize(
        "url,expected",
        [
            (CATALOG_URL, fetchers.CATALOG_BUCKET),
            (f"https://{SHOP}/products.json", fetchers.CATALOG_BUCKET),
            (PAGE_URL, fetchers.PAGE_BUCKET),
            (f"https://{SHOP}/products/eine-box", fetchers.PAGE_BUCKET),
            (f"https://{SHOP}/products/eine-box.js", fetchers.PAGE_BUCKET),
        ],
    )
    def test_only_the_catalogue_endpoint_is_its_own_bucket(self, url, expected):
        assert fetchers.bucket_of(url) == expected


class TestSeparateBudgets:
    def test_a_working_page_no_longer_wipes_the_catalogue_penalty(self):
        """The regression: this is why the pause stayed at 60s forever."""
        fetchers.note_refusal(SHOP, 60.0, fetchers.CATALOG_BUCKET)
        fetchers.note_success(SHOP, fetchers.PAGE_BUCKET)

        # next catalogue refusal must double, not restart
        assert fetchers.note_refusal(SHOP, 60.0, fetchers.CATALOG_BUCKET) == 120.0

    def test_a_refused_catalogue_leaves_pages_alone(self):
        fetchers.note_refusal(SHOP, 60.0, fetchers.CATALOG_BUCKET)

        assert fetchers.cooldown_remaining(SHOP, fetchers.CATALOG_BUCKET) > 0
        assert fetchers.cooldown_remaining(SHOP, fetchers.PAGE_BUCKET) == 0

    def test_a_refused_page_also_pauses_the_catalogue(self):
        """Being turned away on a normal URL means the whole shop is done with us."""
        fetchers.note_refusal(SHOP, 60.0, fetchers.PAGE_BUCKET)

        assert fetchers.cooldown_remaining(SHOP, fetchers.CATALOG_BUCKET) > 0

    def test_a_page_refusal_never_shortens_a_longer_catalogue_pause(self):
        fetchers.note_refusal(SHOP, 600.0, fetchers.CATALOG_BUCKET)
        fetchers.note_refusal(SHOP, 60.0, fetchers.PAGE_BUCKET)

        assert fetchers.cooldown_remaining(SHOP, fetchers.CATALOG_BUCKET) > 300


@pytest.mark.asyncio
class TestThroughFetch:
    async def test_the_bucket_is_derived_from_the_url(self, monkeypatch):
        monkeypatch.setattr(fetchers, "_robots_allowed", AsyncMock(return_value=True))
        monkeypatch.setattr(fetchers.asyncio, "sleep", AsyncMock())

        import httpx

        class Refusing:
            async def get(self, url, **_kw):
                return httpx.Response(
                    429, headers={"Retry-After": "60"}, request=httpx.Request("GET", url)
                )

        monkeypatch.setattr(fetchers, "get_client", lambda: Refusing())

        with pytest.raises(fetchers.RateLimited):
            await fetchers.fetch_httpx(CATALOG_URL, respect_robots=False)

        assert fetchers.cooldown_remaining(SHOP, fetchers.CATALOG_BUCKET) > 0
        assert fetchers.cooldown_remaining(SHOP, fetchers.PAGE_BUCKET) == 0

    async def test_the_message_names_which_budget(self, monkeypatch):
        monkeypatch.setattr(fetchers.asyncio, "sleep", AsyncMock())
        fetchers.note_refusal(SHOP, 60.0, fetchers.CATALOG_BUCKET)

        with pytest.raises(fetchers.RateLimited, match="Katalog"):
            await fetchers.fetch_httpx(CATALOG_URL, respect_robots=False)


@pytest.mark.asyncio
class TestProbeBackoff:
    """The probe itself must ask less and less often until the endpoint answers."""

    async def _refuse_once(self):
        page = fetchers.PageResult(
            url=CATALOG_URL, final_url=CATALOG_URL, status_code=429, text="", json_data=None
        )
        with patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=page)):
            await catalog.catalog(f"https://{SHOP}/products/x")

    async def test_each_failure_doubles_the_wait(self):
        waits = []
        for _ in range(4):
            await self._refuse_once()
            waits.append(catalog._cache[SHOP].ttl)
            catalog._cache.pop(SHOP)  # force the next probe

        assert waits == [120.0, 240.0, 480.0, 960.0]

    async def test_the_wait_is_capped(self):
        for _ in range(8):
            await self._refuse_once()
            catalog._cache.pop(SHOP, None)
        assert catalog._retry_ttl(SHOP) == catalog.MAX_RETRY_TTL_SECONDS

    async def test_a_success_resets_it(self):
        await self._refuse_once()
        catalog._cache.pop(SHOP)

        payload = {"products": [{"handle": "x", "variants": [{"id": 1, "available": True}]}]}
        page = fetchers.PageResult(
            url=CATALOG_URL,
            final_url=CATALOG_URL,
            status_code=200,
            text=json.dumps(payload),
            json_data=payload,
        )
        with patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=page)):
            assert await catalog.catalog(f"https://{SHOP}/products/x")

        assert catalog._retry_ttl(SHOP) == catalog.RETRY_TTL_SECONDS

    async def test_a_settled_non_shopify_shop_is_unaffected(self):
        """That verdict already has its own hour-long TTL."""
        payload = {"nope": True}
        page = fetchers.PageResult(
            url=CATALOG_URL,
            final_url=CATALOG_URL,
            status_code=200,
            text=json.dumps(payload),
            json_data=payload,
        )
        with patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=page)):
            await catalog.catalog(f"https://{SHOP}/products/x")

        assert catalog._cache[SHOP].ttl == catalog.NEGATIVE_TTL_SECONDS
