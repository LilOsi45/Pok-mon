"""Scraping-API fetcher: request building + adapter wiring."""

from __future__ import annotations

import asyncio

import app.config as config
import app.monitor.fetchers as fetchers
from app.monitor.adapters.mediamarkt_de import MediaMarktDeAdapter, SaturnDeAdapter
from app.monitor.adapters.stubs import (
    GamewareAdapter,
    PokemonCenterEuAdapter,
    SmythsToysDeAdapter,
)
from app.monitor.fetchers import _scraper_request


def _reload_settings(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config.get_settings.cache_clear()
    return config.get_settings()


class TestAdapterWiring:
    def test_big_chains_use_scraperapi(self):
        assert MediaMarktDeAdapter().fetcher == "scraperapi"
        assert SaturnDeAdapter().fetcher == "scraperapi"
        assert SmythsToysDeAdapter().fetcher == "scraperapi"
        assert GamewareAdapter().fetcher == "scraperapi"
        assert PokemonCenterEuAdapter().fetcher == "scraperapi"


class TestScraperRequest:
    def test_scraperapi_params(self, monkeypatch):
        _reload_settings(monkeypatch, SCRAPER_API_KEY="secret", SCRAPER_API_PROVIDER="scraperapi")
        endpoint, params = _scraper_request("https://shop.de/p/1")
        assert "api.scraperapi.com" in endpoint
        assert params["api_key"] == "secret"
        assert params["url"] == "https://shop.de/p/1"
        assert params["render"] == "true"
        assert params["country_code"] == "de"
        config.get_settings.cache_clear()

    def test_scrapingbee_params(self, monkeypatch):
        _reload_settings(monkeypatch, SCRAPER_API_KEY="secret", SCRAPER_API_PROVIDER="scrapingbee")
        endpoint, params = _scraper_request("https://shop.de/p/1")
        assert "app.scrapingbee.com" in endpoint
        assert params["render_js"] == "true"
        assert params["country_code"] == "de"
        config.get_settings.cache_clear()

    def test_render_disabled(self, monkeypatch):
        _reload_settings(monkeypatch, SCRAPER_API_KEY="k", SCRAPER_API_RENDER_JS="false")
        _, params = _scraper_request("https://shop.de/p/1")
        assert "render" not in params
        config.get_settings.cache_clear()

    def test_ultra_premium_tier(self, monkeypatch):
        _reload_settings(monkeypatch, SCRAPER_API_KEY="k", SCRAPER_API_TIER="ultra_premium")
        _, params = _scraper_request("https://shop.de/p/1")
        assert params["ultra_premium"] == "true"
        assert "premium" not in params
        config.get_settings.cache_clear()

    def test_standard_tier_has_no_premium_flags(self, monkeypatch):
        _reload_settings(monkeypatch, SCRAPER_API_KEY="k")
        _, params = _scraper_request("https://shop.de/p/1")
        assert "premium" not in params and "ultra_premium" not in params
        config.get_settings.cache_clear()


class _FakeResp:
    def __init__(self, status_code=200, text="<html>ok</html>"):
        self.status_code = status_code
        self.text = text
        self.headers: dict = {}


class _FakeClient:
    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.captured = {"url": url, "json": json, "headers": headers}
        return _FakeResp()


class TestBrightData:
    def test_brightdata_request(self, monkeypatch):
        _reload_settings(
            monkeypatch,
            SCRAPER_API_KEY="tok",
            SCRAPER_API_PROVIDER="brightdata",
            BRIGHTDATA_ZONE="web_unlocker",
        )
        monkeypatch.setattr(fetchers.httpx, "AsyncClient", _FakeClient)
        page = asyncio.run(fetchers.fetch_scraperapi("https://shop.de/p"))
        cap = _FakeClient.captured
        assert cap["url"] == "https://api.brightdata.com/request"
        assert cap["json"]["zone"] == "web_unlocker"
        assert cap["json"]["url"] == "https://shop.de/p"
        assert cap["json"]["format"] == "raw"
        assert cap["json"]["country"] == "de"
        assert cap["headers"]["Authorization"] == "Bearer tok"
        assert page.fetched_via == "scraperapi:brightdata"
        config.get_settings.cache_clear()


class TestBrightDataEmptyBody:
    """An empty unlocker response must fail loudly, not look like a fetched page.

    Bright Data answers HTTP 200 for a successful *API call* — that code says
    nothing about the target. Passing an empty body through as a 200 page turned
    a failed fetch into a silent "0 products found" much further up the chain.
    """

    def test_empty_body_raises_with_diagnostics(self, monkeypatch):
        import httpx
        import pytest

        settings = _reload_settings(
            monkeypatch,
            SCRAPER_API_KEY="token",
            SCRAPER_API_PROVIDER="brightdata",
            BRIGHTDATA_ZONE="web_unlocker1",
        )
        response = httpx.Response(200, text="   ", headers={"x-brd-error": "target_unreachable"})

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, *_args, **_kwargs):
                return response

        monkeypatch.setattr(fetchers.httpx, "AsyncClient", lambda **kw: FakeClient())
        with pytest.raises(fetchers.FetchError) as excinfo:
            asyncio.run(fetchers._fetch_brightdata("https://shop.de/x", settings, 0.0, None))
        message = str(excinfo.value)
        assert "empty body" in message
        assert "x-brd-error=target_unreachable" in message
        config.get_settings.cache_clear()

    def test_real_body_still_comes_through(self, monkeypatch):
        import httpx

        settings = _reload_settings(
            monkeypatch,
            SCRAPER_API_KEY="token",
            SCRAPER_API_PROVIDER="brightdata",
            BRIGHTDATA_ZONE="web_unlocker1",
        )
        response = httpx.Response(200, text="<html>Produkt</html>")

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, *_args, **_kwargs):
                return response

        monkeypatch.setattr(fetchers.httpx, "AsyncClient", lambda **kw: FakeClient())
        page = asyncio.run(fetchers._fetch_brightdata("https://shop.de/x", settings, 0.0, None))
        assert page.status_code == 200 and "Produkt" in page.text
        config.get_settings.cache_clear()


class TestWhoseErrorIsIt:
    """The unlocker's own HTTP status is not the shop's.

    Measured on the queue watch: api.brightdata.com answered 502 with 122 bytes
    of nginx error page. That was handed on as the *shop's* status, and the queue
    adapter reads a 5xx as "the wall is going up, a drop is spinning up" — a
    Discord alert for a Pokémon Center drop that was not happening. The unlocker
    reports the target's real status in x-brd-status-code.
    """

    def _fetch(self, monkeypatch, response):
        settings = _reload_settings(
            monkeypatch,
            SCRAPER_API_KEY="token",
            SCRAPER_API_PROVIDER="brightdata",
            BRIGHTDATA_ZONE="web_unlocker1",
        )

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, *_args, **_kwargs):
                return response

        monkeypatch.setattr(fetchers.httpx, "AsyncClient", lambda **kw: FakeClient())
        try:
            return asyncio.run(
                fetchers._fetch_brightdata(
                    "https://www.pokemoncenter.com/de-de", settings, 0.0, None
                )
            )
        finally:
            config.get_settings.cache_clear()

    def test_a_gateway_502_is_our_failure_not_a_drop(self, monkeypatch):
        import httpx
        import pytest

        nginx = "<html><head><title>502 Bad Gateway</title></head><body></body></html>"
        with pytest.raises(fetchers.FetchError) as excinfo:
            self._fetch(monkeypatch, httpx.Response(502, text=nginx, headers={"server": "nginx"}))
        assert "gateway" in str(excinfo.value)

    def test_the_shops_own_502_is_reported_as_the_shops(self, monkeypatch):
        """With the upstream header there is no ambiguity — pass it through, so
        a real edge failure still reaches the adapter as drop activity."""
        import httpx

        page = self._fetch(
            monkeypatch,
            httpx.Response(
                200, text="<html>edge kaputt</html>", headers={"x-brd-status-code": "502"}
            ),
        )
        assert page.status_code == 502

    def test_a_target_404_is_no_longer_hidden_behind_the_api_200(self, monkeypatch):
        import httpx

        page = self._fetch(
            monkeypatch,
            httpx.Response(
                200, text="<html>not found</html>", headers={"x-brd-status-code": "404"}
            ),
        )
        assert page.status_code == 404

    def test_a_junk_upstream_header_falls_back_to_the_api_status(self, monkeypatch):
        import httpx

        page = self._fetch(
            monkeypatch,
            httpx.Response(200, text="<html>ok</html>", headers={"x-brd-status-code": "keine"}),
        )
        assert page.status_code == 200
