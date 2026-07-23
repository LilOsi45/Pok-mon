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
        _reload_settings(
            monkeypatch, SCRAPER_API_KEY="secret", SCRAPER_API_PROVIDER="scraperapi"
        )
        endpoint, params = _scraper_request("https://shop.de/p/1")
        assert "api.scraperapi.com" in endpoint
        assert params["api_key"] == "secret"
        assert params["url"] == "https://shop.de/p/1"
        assert params["render"] == "true"
        assert params["country_code"] == "de"
        config.get_settings.cache_clear()

    def test_scrapingbee_params(self, monkeypatch):
        _reload_settings(
            monkeypatch, SCRAPER_API_KEY="secret", SCRAPER_API_PROVIDER="scrapingbee"
        )
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
