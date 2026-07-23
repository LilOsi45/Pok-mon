"""Scraping-API fetcher: request building + adapter wiring."""

from __future__ import annotations

import app.config as config
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
