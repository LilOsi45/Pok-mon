"""Channel picker: options come from the notifiers, forms post checkbox lists."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.auth import require_auth
from app.db import get_session
from app.main import app
from app.models import NotifierConfig, ProductScan, Watch
from app.web.routes import _channel_options, merge_channels


@pytest_asyncio.fixture
async def client(session):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_auth] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def notifier(name: str, routes: list[str]) -> NotifierConfig:
    return NotifierConfig(name=name, type="discord", config={}, routes=routes, enabled=True)


class TestMergeChannels:
    def test_checkboxes_and_extra_are_combined(self):
        assert merge_channels(["poke-de"], "hot-drops, tcg-check") == [
            "poke-de",
            "hot-drops",
            "tcg-check",
        ]

    def test_duplicates_collapse_and_blanks_drop(self):
        assert merge_channels(["poke-de", "poke-de"], " , poke-de , neu ") == ["poke-de", "neu"]

    def test_nothing_selected_is_empty(self):
        assert merge_channels([], "") == []


@pytest.mark.asyncio
class TestChannelOptions:
    async def test_only_channel_routes_are_offered(self, session):
        session.add_all(
            [
                notifier("pc-alarm", ["channel:pc-queue"]),
                notifier("poke", ["stock:pokemon", "channel:poke-de"]),
                notifier("news", ["news:*"]),  # not a channel route
            ]
        )
        await session.commit()
        assert await _channel_options(session) == [
            ("pc-queue", "pc-alarm"),
            ("poke-de", "poke"),
        ]

    async def test_same_tag_on_two_notifiers_lists_both(self, session):
        session.add_all(
            [notifier("first", ["channel:shared"]), notifier("second", ["channel:shared"])]
        )
        await session.commit()
        assert await _channel_options(session) == [("shared", "first, second")]

    async def test_wildcard_and_empty_tags_are_skipped(self, session):
        session.add(notifier("wild", ["channel:*", "channel:", "channel:real"]))
        await session.commit()
        assert await _channel_options(session) == [("real", "wild")]

    async def test_disabled_notifier_is_not_offered(self, session):
        row = notifier("off", ["channel:hidden"])
        row.enabled = False
        session.add(row)
        await session.commit()
        assert await _channel_options(session) == []


@pytest.mark.asyncio
class TestWatchFormPost:
    async def test_multiple_ticked_boxes_are_all_saved(self, client, session):
        with patch("app.web.routes.schedule_watch"):
            resp = await client.post(
                "/watches",
                data={
                    "game": "pokemon",
                    "label": "Pokémon Center UK",
                    "url": "https://www.pokemoncenter.com/en-gb",
                    "channels": ["pc-queue", "poke-usgb"],
                    "channels_extra": "tcg-check",
                },
            )
        assert resp.status_code == 303
        watch = (await session.execute(select(Watch))).scalars().one()
        assert watch.channels == ["pc-queue", "poke-usgb", "tcg-check"]

    async def test_no_box_ticked_falls_back_to_the_game(self, client, session):
        with patch("app.web.routes.schedule_watch"):
            resp = await client.post(
                "/watches",
                data={"game": "one_piece", "label": "OP", "url": "https://example.com/op"},
            )
        assert resp.status_code == 303
        watch = (await session.execute(select(Watch))).scalars().one()
        assert watch.channels == ["one_piece"]

    async def test_edit_form_preticks_the_saved_channels(self, client, session):
        session.add(notifier("poke", ["channel:poke-de"]))
        watch = Watch(
            game="pokemon",
            label="Watch",
            url="https://example.com/p",
            channels=["poke-de", "eigener-tag"],
        )
        session.add(watch)
        await session.commit()

        resp = await client.get(f"/watches/{watch.id}/edit")
        assert resp.status_code == 200
        # known tag -> ticked checkbox; unknown tag -> kept in the free-text box
        assert 'value="poke-de" checked' in resp.text
        assert 'value="eigener-tag"' in resp.text


@pytest.mark.asyncio
class TestScanFormPost:
    async def test_scanner_saves_ticked_channels(self, client, session):
        with patch("app.web.routes.schedule_scan"):
            resp = await client.post(
                "/scanner",
                data={
                    "game": "pokemon",
                    "label": "Smyths Neuheiten",
                    "url": "https://example.com/neu",
                    "keywords": "pokemon",
                    "channels": ["poke-de"],
                },
            )
        assert resp.status_code == 303
        scan = (await session.execute(select(ProductScan))).scalars().one()
        assert scan.channels == ["poke-de"]
