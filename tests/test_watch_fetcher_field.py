"""The fetch method must be settable from the dashboard.

A Pokémon Center queue watch is blind unless it runs through the unlocker, and
that route lives in the JSON `detection` column. The column had no input field
at all, so the one setting that decides whether the watch can see a drop was
reachable only by editing the database — the operator was told to fill in a
field that did not exist. These tests pin the field, the save, and the fact that
other detection keys survive it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.auth import require_auth
from app.db import get_session
from app.main import app
from app.models import Watch
from app.monitor.registry import resolve_adapter
from app.web.routes import _fetcher_choice

FORM = Path("app/web/templates/_watch_form.html")


@pytest_asyncio.fixture
async def client(session):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_auth] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def base_form(**extra) -> dict:
    return {
        "game": "pokemon",
        "label": "Pokémon Center Queue-Alarm DE",
        "url": "https://www.pokemoncenter.com/de-de",
        "adapter": "pokemon_center_queue",
        **extra,
    }


class TestTheFieldExists:
    def test_the_form_offers_a_fetcher_select(self):
        assert 'name="fetcher"' in FORM.read_text(encoding="utf-8")


class TestFetcherChoice:
    def test_the_provider_name_maps_to_the_unlocker(self):
        assert _fetcher_choice("brightdata") == "scraperapi"

    def test_known_values_pass(self):
        assert _fetcher_choice("playwright") == "playwright"
        assert _fetcher_choice("scraperapi") == "scraperapi"

    def test_empty_and_junk_mean_default(self):
        assert _fetcher_choice("") == ""
        assert _fetcher_choice("nonsense") == ""


@pytest.mark.asyncio
class TestSaving:
    async def test_creating_a_watch_stores_the_choice(self, client, session):
        with patch("app.web.routes.schedule_watch"):
            resp = await client.post("/watches", data=base_form(fetcher="scraperapi"))
        assert resp.status_code == 303
        watch = (await session.execute(select(Watch))).scalars().one()
        assert watch.detection == {"fetcher": "scraperapi"}

    async def test_editing_can_clear_it_again(self, client, session):
        watch = Watch(
            game="pokemon",
            label="Q",
            url="https://www.pokemoncenter.com/de-de",
            detection={"fetcher": "scraperapi"},
        )
        session.add(watch)
        await session.commit()

        with patch("app.web.routes.schedule_watch"):
            resp = await client.post(f"/watches/{watch.id}", data=base_form(fetcher=""))
        assert resp.status_code == 303
        await session.refresh(watch)
        assert watch.detection == {}

    async def test_other_detection_keys_survive(self, client, session):
        """Cardmarket watches keep their own keys in the same column."""
        watch = Watch(
            game="pokemon",
            label="CM",
            url="https://www.cardmarket.com/de/x",
            detection={"id_product": 771234, "min_articles": 3},
        )
        session.add(watch)
        await session.commit()

        with patch("app.web.routes.schedule_watch"):
            await client.post(f"/watches/{watch.id}", data=base_form(fetcher="playwright"))
        await session.refresh(watch)
        assert watch.detection == {
            "id_product": 771234,
            "min_articles": 3,
            "fetcher": "playwright",
        }

    async def test_the_edit_form_preselects_the_saved_choice(self, client, session):
        watch = Watch(
            game="pokemon",
            label="Q",
            url="https://www.pokemoncenter.com/de-de",
            detection={"fetcher": "scraperapi"},
        )
        session.add(watch)
        await session.commit()

        resp = await client.get(f"/watches/{watch.id}/edit")
        assert 'value="scraperapi" selected' in resp.text


class TestTheAdapterActuallyUsesIt:
    def test_the_unlocker_choice_reaches_a_normal_adapter(self):
        """An unknown fetcher string falls back to httpx without complaining."""
        adapter = resolve_adapter("https://shop.de/p", None, {"fetcher": "scraperapi"})
        assert adapter.fetcher == "scraperapi"

    def test_the_old_provider_spelling_is_translated(self):
        adapter = resolve_adapter("https://shop.de/p", None, {"fetcher": "brightdata"})
        assert adapter.fetcher == "scraperapi"
