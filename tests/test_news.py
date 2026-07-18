"""News parsing, dedupe, and event flag logic."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.config import NewsSourceConfig
from app.models import Game, NewsStatus, SetNews, utcnow
from app.monitor.base import PageResult
from app.news.base import NewsItem
from app.news.service import ingest_items, scan_release_soon
from app.news.sources.onepiece_official import OnePieceOfficialSource
from app.news.sources.rss import RSSSource, guess_release_date
from tests.conftest import load_fixture


def _page(text: str, url: str = "https://example.com/feed") -> PageResult:
    return PageResult(url=url, final_url=url, status_code=200, text=text)


class TestRSSSource:
    @pytest.mark.asyncio
    async def test_parses_and_filters_relevant_items(self):
        cfg = NewsSourceConfig(name="pokebeach", type="rss", url="https://x/feed", game="pokemon")
        source = RSSSource(cfg)
        with patch(
            "app.monitor.fetchers.fetch_httpx",
            AsyncMock(return_value=_page(load_fixture("rss_pokebeach.xml"))),
        ):
            items = await source.fetch_items()
        titles = [i.title for i in items]
        # set reveal kept; player interview filtered out (no set keywords)
        assert any("Mega Evolution" in t for t in titles)
        assert not any("Spotlight" in t for t in titles)
        mega = next(i for i in items if "Mega Evolution" in i.title)
        assert mega.game == Game.POKEMON
        assert mega.release_date == date(2025, 11, 14)
        assert mega.image_url == "https://www.pokebeach.com/news/mega-evolution.jpg"

    @pytest.mark.asyncio
    async def test_both_mode_classifies_per_item(self):
        cfg = NewsSourceConfig(name="mixed", type="rss", url="https://x/feed", game="both")
        source = RSSSource(cfg)
        with patch(
            "app.monitor.fetchers.fetch_httpx",
            AsyncMock(return_value=_page(load_fixture("rss_pokebeach.xml"))),
        ):
            items = await source.fetch_items()
        games = {i.title[:20]: i.game for i in items}
        assert any(g == Game.ONE_PIECE for g in games.values())
        assert any(g == Game.POKEMON for g in games.values())


class TestOnePieceOfficial:
    @pytest.mark.asyncio
    async def test_parses_product_entries_only(self):
        cfg = NewsSourceConfig(
            name="op-official",
            type="onepiece_official",
            url="https://en.onepiece-cardgame.com/information/",
            game="one_piece",
        )
        source = OnePieceOfficialSource(cfg)
        with patch(
            "app.monitor.fetchers.fetch_httpx",
            AsyncMock(return_value=_page(load_fixture("onepiece_news.html"))),
        ):
            items = await source.fetch_items()
        assert len(items) == 2  # two PRODUCTS entries; events + maintenance skipped
        op12 = next(i for i in items if "OP-12" in i.title)
        assert op12.game == Game.ONE_PIECE
        assert op12.release_date == date(2025, 10, 24)


class TestIngest:
    def _item(self, guid: str = "g1", title: str = "New Booster Set") -> NewsItem:
        return NewsItem(
            game=Game.POKEMON, title=title, url=f"https://x/{guid}", guid=guid, source="src"
        )

    @pytest.mark.asyncio
    async def test_dedupes_on_source_guid(self, session):
        created = await ingest_items(session, [self._item(), self._item()])
        assert len(created) == 1
        created = await ingest_items(session, [self._item()])
        assert created == []
        rows = (await session.execute(select(SetNews))).scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_preorder_marker_sets_status(self, session):
        created = await ingest_items(
            session, [self._item(guid="g2", title="OP-12 Pre-Order now live!")]
        )
        assert created[0].status == NewsStatus.PREORDER


class TestReleaseSoon:
    @pytest.mark.asyncio
    async def test_flags_only_inside_window(self, session):
        soon = SetNews(
            game=Game.POKEMON,
            title="Soon Set",
            source="s",
            guid="a",
            url="https://x/a",
            release_date=(utcnow() + timedelta(days=3)).date(),
        )
        far = SetNews(
            game=Game.POKEMON,
            title="Far Set",
            source="s",
            guid="b",
            url="https://x/b",
            release_date=(utcnow() + timedelta(days=60)).date(),
        )
        past = SetNews(
            game=Game.POKEMON,
            title="Past Set",
            source="s",
            guid="c",
            url="https://x/c",
            release_date=(utcnow() - timedelta(days=3)).date(),
        )
        session.add_all([soon, far, past])
        await session.commit()
        with patch("app.news.service.dispatch_event", AsyncMock()) as dispatched:
            await scan_release_soon(session)
        assert soon.release_soon_notified is True
        assert far.release_soon_notified is False
        assert past.release_soon_notified is False
        assert dispatched.call_count == 1

    def test_guess_release_date(self):
        assert guess_release_date("launches March 27, 2026") == date(2026, 3, 27)
        assert guess_release_date("Available November 14th, 2025!") == date(2025, 11, 14)
        assert guess_release_date("no date here") is None
