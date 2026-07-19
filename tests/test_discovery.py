"""Shop discovery: search parsing, vetting heuristics, hunt end-to-end."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.discovery import (
    SearchHit,
    _score_homepage,
    is_excluded_domain,
    normalize_domain,
    parse_ddg_html,
    parse_trustpilot,
    run_hunt,
    vet_site,
)
from app.models import DiscoveredSite, DiscoveryHunt, EventType, Game
from app.monitor.base import PageResult
from tests.conftest import load_fixture


def _page(text: str, url: str = "https://x/", status: int = 200) -> PageResult:
    return PageResult(url=url, final_url=url, status_code=status, text=text)


class TestSearchParsing:
    def test_ddg_results_unwrapped(self):
        hits = parse_ddg_html(load_fixture("ddg_results.html"))
        urls = [h.url for h in hits]
        assert "https://www.neuer-kartenshop.de/pokemon/kp12-booster-display" in urls
        assert "https://www.games-island.eu/pokemon/kp12-display" in urls  # direct link kept
        assert all(u.startswith("http") for u in urls)
        first = hits[0]
        assert "Neuer Kartenshop" in first.title

    def test_excluded_domains(self):
        assert is_excluded_domain("www.ebay.de".removeprefix("www."))
        assert is_excluded_domain("games-island.eu")  # catalog shop, already covered
        assert is_excluded_domain("shop.amazon.de")
        assert not is_excluded_domain("neuer-kartenshop.de")

    def test_normalize_domain(self):
        assert normalize_domain("https://WWW.Shop.DE/p/1?x=1") == "shop.de"


class TestVetting:
    def test_good_homepage_scores_high(self):
        score, checks = _score_homepage(load_fixture("homepage_good.html"))
        assert checks["impressum"] is True
        assert checks["payment"] == "paypal/klarna"
        assert score >= 6

    def test_bad_homepage_scores_low(self):
        score, checks = _score_homepage(load_fixture("homepage_bad.html"))
        assert checks["impressum"] is False
        assert checks["payment"] == "nur vorkasse?"
        assert score <= -4

    def test_trustpilot_jsonld(self):
        rating, count = parse_trustpilot(load_fixture("trustpilot_page.html"))
        assert rating == 4.6
        assert count == 312

    @pytest.mark.asyncio
    async def test_vet_site_verdicts(self):
        async def fake_fetch(url, **kw):
            return _page(load_fixture("homepage_good.html"), url=url)

        class FakeResp:
            status_code = 200
            text = load_fixture("trustpilot_page.html")

        class FakeClient:
            def __init__(self, *a, **kw): ...
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, **kw):
                return FakeResp()

        with (
            patch("app.monitor.fetchers.fetch_httpx", AsyncMock(side_effect=fake_fetch)),
            patch("app.discovery.httpx.AsyncClient", FakeClient),
        ):
            check = await vet_site("neuer-kartenshop.de")
        assert check.verdict == "trusted"
        assert check.checks["trustpilot_rating"] == 4.6


def _hits() -> list[SearchHit]:
    return [
        SearchHit(url="https://www.neuer-kartenshop.de/pokemon/kp12", title="KP12 Display kaufen"),
        SearchHit(url="https://www.games-island.eu/p/kp12", title="KP12 - Games Island"),  # catalog
        SearchHit(url="https://www.ebay.de/itm/1", title="KP12 bei eBay"),  # excluded
    ]


@pytest.mark.asyncio
async def test_run_hunt_end_to_end(session):
    hunt = DiscoveryHunt(game=Game.POKEMON, label="KP12 Suche", query="kp12 display kaufen")
    session.add(hunt)
    await session.commit()

    async def fake_fetch(url, **kw):
        return _page(load_fixture("homepage_good.html"), url=url)

    good_check = AsyncMock()
    good_check.return_value.verdict = "trusted"
    good_check.return_value.score = 9
    good_check.return_value.checks = {"impressum": True}

    with (
        patch("app.discovery.search_web", AsyncMock(return_value=_hits())),
        patch("app.monitor.fetchers.fetch_httpx", AsyncMock(side_effect=fake_fetch)),
        patch("app.discovery.vet_site", good_check),
        patch("app.discovery.dispatch_event", AsyncMock()) as dispatched,
    ):
        created = await run_hunt(session, hunt)

    # only the genuinely new domain survives (catalog + ebay filtered)
    assert [s.domain for s in created] == ["neuer-kartenshop.de"]
    assert created[0].notified is True
    assert dispatched.call_count == 1
    event = dispatched.call_args.args[1]
    assert event.type == EventType.NEW_SHOP_FOUND
    assert "neuer-kartenshop.de" in event.title
    assert event.url == "https://www.neuer-kartenshop.de/pokemon/kp12"

    # second run: domain is known globally -> nothing new, no ping
    with (
        patch("app.discovery.search_web", AsyncMock(return_value=_hits())),
        patch("app.discovery.dispatch_event", AsyncMock()) as dispatched,
    ):
        assert await run_hunt(session, hunt) == []
    assert dispatched.call_count == 0


@pytest.mark.asyncio
async def test_suspicious_site_stored_but_not_pinged(session):
    hunt = DiscoveryHunt(game=Game.POKEMON, label="Suche", query="kp12 kaufen")
    session.add(hunt)
    await session.commit()

    bad_check = AsyncMock()
    bad_check.return_value.verdict = "suspicious"
    bad_check.return_value.score = -6
    bad_check.return_value.checks = {"impressum": False}

    hits = [SearchHit(url="https://www.billig-karten.top/kp12", title="KP12 79,99€!!!")]
    with (
        patch("app.discovery.search_web", AsyncMock(return_value=hits)),
        patch(
            "app.monitor.fetchers.fetch_httpx",
            AsyncMock(return_value=_page(load_fixture("homepage_bad.html"))),
        ),
        patch("app.discovery.vet_site", bad_check),
        patch("app.discovery.dispatch_event", AsyncMock()) as dispatched,
    ):
        created = await run_hunt(session, hunt)

    assert len(created) == 1
    assert created[0].verdict == "suspicious"
    assert created[0].notified is False
    assert dispatched.call_count == 0  # never pushed to Discord
    rows = (await session.execute(select(DiscoveredSite))).scalars().all()
    assert len(rows) == 1  # but visible in the dashboard


@pytest.mark.asyncio
async def test_search_failure_recorded(session):
    hunt = DiscoveryHunt(game=Game.POKEMON, label="Suche", query="x")
    session.add(hunt)
    await session.commit()
    with patch("app.discovery.search_web", AsyncMock(side_effect=RuntimeError("HTTP 429"))):
        assert await run_hunt(session, hunt) == []
    assert "429" in hunt.last_error
