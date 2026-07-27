"""A diagnostic must not change what it measures.

The capacity report used to probe every shop itself, from a process that knew
nothing about the app's cooldowns. So each run fired a fresh request at all 15
shops and re-armed the rate limit whose expiry it was supposed to observe — the
report kept showing "Katalog 0" partly because running it prevented recovery.

It now reads the live app state over localhost and contacts nobody.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.capacity_report import Domain, _apply, collect
from app.monitor import catalog, fetchers


@pytest.fixture(autouse=True)
def _clean():
    catalog.clear_cache()
    fetchers.reset_cooldowns()
    yield
    catalog.clear_cache()
    fetchers.reset_cooldowns()


def _client(host: str = "127.0.0.1") -> TestClient:
    from app.main import app

    # TestClient's default client address is the literal "testclient", which the
    # localhost guard rightly rejects.
    return TestClient(app, client=(host, 51234))


class TestEndpoint:
    def test_it_reports_a_loaded_catalog(self):
        catalog._cache["geeksheaven.de"] = catalog._Entry(
            at=0.0, index={"a": {}, "b": {}}, reason="2 Produkte", ttl=45.0
        )
        body = _client().get("/healthz/shops").json()

        shop = body["shops"]["geeksheaven.de"]["catalog"]
        assert shop["products"] == 2
        assert shop["handles"] == ["a", "b"]
        assert shop["reason"] == "2 Produkte"

    def test_it_reports_an_active_cooldown(self):
        fetchers.note_refusal("geeksheaven.de", 60.0)
        body = _client().get("/healthz/shops").json()

        cooldown = body["shops"]["geeksheaven.de"]["cooldown"]["page"]
        assert 55 <= cooldown["remaining_seconds"] <= 60
        assert cooldown["consecutive_refusals"] == 1

    def test_an_untouched_install_reports_nothing(self):
        assert _client().get("/healthz/shops").json() == {"shops": {}}

    def test_a_remote_client_gets_a_404(self):
        """Behind the reverse proxy the client address is not 127.0.0.1."""
        assert _client("203.0.113.9").get("/healthz/shops").status_code == 404

    def test_localhost_is_allowed(self):
        assert _client("127.0.0.1").get("/healthz/shops").status_code == 200

    def test_plain_healthz_still_works(self):
        assert _client().get("/healthz").status_code in (200, 503)


class TestReportUsesTheAppState:
    def test_handles_decide_what_the_catalog_covers(self):
        d = Domain(
            host="geeksheaven.de",
            items=[
                ("https://geeksheaven.de/products/drin", 120, True),
                ("https://geeksheaven.de/products/nicht-drin", 120, True),
            ],
        )
        _apply(d, {"geeksheaven.de": {"catalog": {"handles": ["drin"], "reason": "1 Produkte"}}})

        assert len(d.served_by_catalog) == 1
        assert len(d.own_request) == 1

    def test_a_cooldown_is_shown_instead_of_a_stale_reason(self):
        d = Domain(host="shop.de", items=[("https://shop.de/products/x", 120, True)])
        _apply(
            d,
            {
                "shop.de": {
                    "catalog": {"handles": [], "reason": "HTTP 429"},
                    "cooldown": {
                        "catalog": {"remaining_seconds": 240.0, "consecutive_refusals": 3},
                        "page": {"remaining_seconds": 0, "consecutive_refusals": 0},
                    },
                }
            },
        )

        assert "Pause 240s" in d.probe_error
        assert "3x abgewiesen" in d.probe_error

    def test_a_www_host_still_matches(self):
        d = Domain(host="shop.de", items=[("https://www.shop.de/products/x", 120, True)])
        _apply(d, {"www.shop.de": {"catalog": {"handles": ["x"], "reason": "1 Produkte"}}})
        assert len(d.served_by_catalog) == 1

    def test_an_unknown_shop_says_so(self):
        d = Domain(host="neu.de", items=[("https://neu.de/products/x", 120, True)])
        _apply(d, {})
        assert d.probe_error == "noch nicht geprüft"

    @pytest.mark.asyncio
    async def test_collect_contacts_no_shop(self, session, watch):
        """The whole point: running the report must send zero shop requests."""
        session.add(watch)
        await session.commit()

        boom = AsyncMock(side_effect=AssertionError("a shop was contacted"))
        with (
            patch("app.monitor.fetchers.fetch_httpx", boom),
            patch("app.capacity_report.get_sessionmaker", lambda: lambda: _Reuse(session)),
        ):
            await collect()

        boom.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_stopped_app_is_reported_plainly(self, session, watch):
        session.add(watch)
        await session.commit()

        with patch("app.capacity_report.get_sessionmaker", lambda: lambda: _Reuse(session)):
            domains = await collect()

        assert domains
        assert "App nicht erreichbar" in domains[0].probe_error


class _Reuse:
    """Hand the test's own session to code that opens its own."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False
