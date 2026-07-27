"""Shops answer the program, not just the address.

Measured on the live server against geeksheaven.de, five requests seconds apart:

    httpx, app headers          429   18 B   server=cloudflare
    httpx, without Cache-Control 429  18 B   server=cloudflare
    httpx, curl's exact headers 429   18 B   server=cloudflare
    curl                        200   936 KB

Every httpx variant failed and the one request that differed in *program*
succeeded, so no header, delay or proxy could have fixed it — Cloudflare
fingerprints the TLS handshake, and that fingerprint travels with us through any
proxy. curl_cffi sends a real Chrome handshake instead.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.monitor import browser_tls, fetchers


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    browser_tls.reset_for_tests()
    monkeypatch.setenv("BROWSER_TLS", "true")
    import app.config as config

    config.get_settings.cache_clear()
    yield
    browser_tls.reset_for_tests()
    config.get_settings.cache_clear()


class _Resp:
    def __init__(self, status=200, text="hi", headers=None, url="https://shop.de/x"):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self.url = url
        self.elapsed = 0.25

    def json(self):
        import json

        return json.loads(self.text)


class TestItIsInstalled:
    def test_the_library_is_available(self):
        """It is a hard dependency — without it every Cloudflare shop stays dark."""
        assert browser_tls.available() is True

    def test_it_impersonates_a_browser(self):
        assert browser_tls.IMPERSONATE.startswith("chrome")


@pytest.mark.asyncio
class TestFetch:
    async def test_it_returns_a_normal_page_result(self):
        session = AsyncMock()
        session.get.return_value = _Resp(text="<h1>Box</h1>")
        with patch.object(browser_tls, "_get_session", lambda _p: session):
            page = await browser_tls.fetch("https://shop.de/x")

        assert page.status_code == 200
        assert page.text == "<h1>Box</h1>"
        assert page.fetched_via == "browser-tls"

    async def test_json_bodies_are_parsed(self):
        session = AsyncMock()
        session.get.return_value = _Resp(
            text='{"products": []}', headers={"content-type": "application/json"}
        )
        with patch.object(browser_tls, "_get_session", lambda _p: session):
            page = await browser_tls.fetch("https://shop.de/products.json")

        assert page.json_data == {"products": []}

    async def test_a_broken_json_body_is_not_fatal(self):
        session = AsyncMock()
        session.get.return_value = _Resp(
            text="not json", headers={"content-type": "application/json"}
        )
        with patch.object(browser_tls, "_get_session", lambda _p: session):
            page = await browser_tls.fetch("https://shop.de/products.json")

        assert page.json_data is None
        assert page.status_code == 200


class TestSessionsPerRoute:
    """Regression: one cached session ignored the proxy argument entirely."""

    def test_direct_and_proxied_use_different_sessions(self):
        made: list[str | None] = []

        class FakeRequests:
            @staticmethod
            def AsyncSession(**kw):  # noqa: N802 - mirrors curl_cffi's name
                made.append((kw.get("proxies") or {}).get("https"))
                return object()

        with patch.dict("sys.modules", {"curl_cffi": type("m", (), {"requests": FakeRequests})}):
            browser_tls._get_session(None)
            browser_tls._get_session("http://proxy:8000")
            browser_tls._get_session(None)  # cached, not rebuilt

        assert made == [None, "http://proxy:8000"]


@pytest.mark.asyncio
class TestFetchersUseIt:
    async def test_fetch_httpx_routes_through_the_browser_client(self, monkeypatch):
        """The whole point — the default path must be the one shops answer."""
        monkeypatch.setattr(fetchers, "_robots_allowed", AsyncMock(return_value=True))
        monkeypatch.setattr(fetchers.asyncio, "sleep", AsyncMock())
        called: list[str] = []

        async def fake(url, *, extra_headers=None, proxy=None):
            called.append(url)
            return fetchers.PageResult(
                url=url, final_url=url, status_code=200, text="ok", fetched_via="browser-tls"
            )

        monkeypatch.setattr(browser_tls, "fetch", fake)

        page = await fetchers.fetch_httpx("https://shop.de/x", respect_robots=False)

        assert called == ["https://shop.de/x"]
        assert page.fetched_via == "browser-tls"

    async def test_it_falls_back_to_httpx_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(fetchers, "_robots_allowed", AsyncMock(return_value=True))
        monkeypatch.setattr(fetchers.asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(browser_tls, "available", lambda: False)

        import httpx

        class Client:
            async def get(self, url, **_kw):
                return httpx.Response(200, text="ok", request=httpx.Request("GET", url))

        monkeypatch.setattr(fetchers, "get_client", lambda *_a, **_k: Client())

        page = await fetchers.fetch_httpx("https://shop.de/x", respect_robots=False)

        assert page.fetched_via == "httpx"

    async def test_a_refusal_is_still_handled_the_same_way(self, monkeypatch):
        """Cooldowns must not care which client collected the 429."""
        monkeypatch.setattr(fetchers, "_robots_allowed", AsyncMock(return_value=True))
        monkeypatch.setattr(fetchers.asyncio, "sleep", AsyncMock())
        fetchers.reset_cooldowns()

        async def refusing(url, *, extra_headers=None, proxy=None):
            return fetchers.PageResult(
                url=url,
                final_url=url,
                status_code=429,
                text="",
                headers={"Retry-After": "60"},
                fetched_via="browser-tls",
            )

        monkeypatch.setattr(browser_tls, "fetch", refusing)

        with pytest.raises(fetchers.RateLimited):
            await fetchers.fetch_httpx("https://shop.de/x", respect_robots=False)

        assert fetchers.cooldown_remaining("shop.de") > 0
        fetchers.reset_cooldowns()
