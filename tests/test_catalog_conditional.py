"""Ask the shop to skip the body when nothing changed.

geeksheaven's catalogue measured 936 KB in the live curl test. Fetched every
45 s that is ~900 MB a day for one shop — a 10 GB monthly proxy allowance would
be gone in eleven days, and the tracker would spend the rest of the month on the
address the shops already refuse.

A conditional request costs a few hundred bytes when the catalogue is unchanged,
which is almost always. The saving is what makes running everything through a
metered proxy affordable at all.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.monitor import catalog, fetchers

SHOP = "https://geeksheaven.de"
HOST = "geeksheaven.de"
PRODUCT = f"{SHOP}/products/box"

PAYLOAD = {"products": [{"handle": "box", "variants": [{"id": 1, "available": True}]}]}


@pytest.fixture(autouse=True)
def _clean():
    catalog.clear_cache()
    yield
    catalog.clear_cache()


def _page(status=200, *, payload=None, headers=None):
    body = json.dumps(payload) if payload is not None else ""
    return fetchers.PageResult(
        url=f"{SHOP}/products.json?limit=250",
        final_url=f"{SHOP}/products.json?limit=250",
        status_code=status,
        text=body,
        json_data=payload,
        headers=headers or {},
    )


@pytest.mark.asyncio
class TestValidatorsAreSent:
    async def test_the_first_fetch_sends_none(self):
        fetch = AsyncMock(return_value=_page(payload=PAYLOAD, headers={"ETag": '"abc"'}))
        with patch("app.monitor.fetchers.fetch_httpx", fetch):
            await catalog.catalog(PRODUCT)

        assert fetch.await_args.kwargs["extra_headers"] is None

    async def test_the_next_fetch_sends_the_etag(self):
        fetch = AsyncMock(return_value=_page(payload=PAYLOAD, headers={"ETag": '"abc"'}))
        with patch("app.monitor.fetchers.fetch_httpx", fetch):
            await catalog.catalog(PRODUCT)
            catalog._cache.pop(HOST + "x", None)
            catalog._cache[HOST] = catalog._Entry(
                at=0.0,  # stale, forces a refetch
                index=catalog._cache[HOST].index,
                reason="x",
                ttl=0.0,
                etag='"abc"',
            )
            await catalog.catalog(PRODUCT)

        assert fetch.await_args.kwargs["extra_headers"] == {"If-None-Match": '"abc"'}

    async def test_last_modified_is_used_too(self):
        stamp = "Wed, 21 Oct 2026 07:28:00 GMT"
        fetch = AsyncMock(return_value=_page(payload=PAYLOAD))
        catalog._cache[HOST] = catalog._Entry(
            at=0.0, index={"box": {}}, reason="x", ttl=0.0, last_modified=stamp
        )
        with patch("app.monitor.fetchers.fetch_httpx", fetch):
            await catalog.catalog(PRODUCT)

        assert fetch.await_args.kwargs["extra_headers"] == {"If-Modified-Since": stamp}

    async def test_a_failed_shop_does_not_send_stale_validators(self):
        """Nothing was cached, so there is nothing to validate against."""
        catalog._cache[HOST] = catalog._Entry(
            at=0.0, index=None, reason="429", ttl=0.0, etag='"old"'
        )
        fetch = AsyncMock(return_value=_page(payload=PAYLOAD))
        with patch("app.monitor.fetchers.fetch_httpx", fetch):
            await catalog.catalog(PRODUCT)

        assert fetch.await_args.kwargs["extra_headers"] is None


@pytest.mark.asyncio
class TestNotModified:
    async def _seed(self) -> None:
        with patch(
            "app.monitor.fetchers.fetch_httpx",
            AsyncMock(return_value=_page(payload=PAYLOAD, headers={"ETag": '"abc"'})),
        ):
            await catalog.catalog(PRODUCT)
        old = catalog._cache[HOST]
        catalog._cache[HOST] = catalog._Entry(
            at=0.0, index=old.index, reason=old.reason, ttl=0.0, etag=old.etag
        )

    async def test_304_keeps_the_catalogue(self):
        await self._seed()
        with patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=_page(304))):
            index = await catalog.catalog(PRODUCT)

        assert index == {"box": {"handle": "box", "variants": [{"id": 1, "available": True}]}}

    async def test_304_refreshes_the_ttl(self):
        await self._seed()
        with patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=_page(304))):
            await catalog.catalog(PRODUCT)

        entry = catalog._cache[HOST]
        assert entry.fresh(entry.at), "a 304 must count as a fresh catalogue"
        assert "unverändert" in entry.reason

    async def test_304_keeps_the_validators_for_next_time(self):
        await self._seed()
        with patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=_page(304))):
            await catalog.catalog(PRODUCT)

        assert catalog._cache[HOST].etag == '"abc"'

    async def test_a_304_without_anything_cached_is_not_treated_as_a_catalogue(self):
        with patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=_page(304))):
            assert await catalog.catalog(PRODUCT) is None

    async def test_a_changed_catalogue_replaces_the_old_one(self):
        await self._seed()
        new = {"products": [{"handle": "neu", "variants": [{"id": 2, "available": False}]}]}
        with patch(
            "app.monitor.fetchers.fetch_httpx",
            AsyncMock(return_value=_page(payload=new, headers={"ETag": '"def"'})),
        ):
            index = await catalog.catalog(PRODUCT)

        assert set(index) == {"neu"}
        assert catalog._cache[HOST].etag == '"def"'


class TestBilledBytes:
    """A gzipped body bills for what crossed the wire, not what we decoded."""

    def test_content_length_wins_over_the_decoded_text(self):
        page = fetchers.PageResult(
            url="u",
            final_url="u",
            status_code=200,
            text="x" * 900_000,
            headers={"content-length": "120000"},
        )
        assert fetchers._wire_bytes(page) == 120_000

    def test_it_falls_back_to_the_body_length(self):
        page = fetchers.PageResult(url="u", final_url="u", status_code=200, text="abc")
        assert fetchers._wire_bytes(page) == 3

    def test_a_nonsense_header_does_not_crash(self):
        page = fetchers.PageResult(
            url="u", final_url="u", status_code=200, text="abc", headers={"content-length": "viel"}
        )
        assert fetchers._wire_bytes(page) == 3

    def test_a_304_costs_almost_nothing(self):
        page = fetchers.PageResult(url="u", final_url="u", status_code=304, text="")
        assert fetchers._wire_bytes(page) == 0
