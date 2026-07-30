"""Changing a scanner's URL must not announce the new page as drops.

Scanner 14 watched the wrong Pokémon Center category for weeks. Correcting the
URL is the fix — but the remembered products belong to the old page, so every
product on the new one counts as unseen and the next run would fire a whole
category as fresh drops. An alert channel that cries wolf stops being read,
which is the failure this tracker exists to avoid.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.auth import require_auth
from app.db import get_session
from app.main import app
from app.models import Game, ProductScan, ScanItem

OLD_URL = "https://www.pokemoncenter.com/en-gb/category/elite-trainer-box"
NEW_URL = "https://www.pokemoncenter.com/de-de/category/new-releases?category=tcg-cards"


@pytest_asyncio.fixture
async def client(session):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_auth] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def scan(session):
    row = ProductScan(
        game=Game.POKEMON,
        label="Pokémoncenter Neuheiten",
        url=OLD_URL,
        keywords=["p"],
        channels=["pc-queue"],
        baseline_done=True,
    )
    session.add(row)
    await session.commit()
    session.add_all(
        [
            ScanItem(
                scan_id=row.id,
                url=f"{OLD_URL}/alt-{n}",
                title=f"Altes Produkt {n}",
                url_key=f"alt-{n}",
                notified=True,
            )
            for n in range(3)
        ]
    )
    await session.commit()
    return row


def form(**extra) -> dict:
    return {
        "game": "pokemon",
        "label": "Pokémoncenter Neuheiten",
        "url": OLD_URL,
        "keywords": "",
        "channels": ["pc-queue"],
        **extra,
    }


async def item_count(session, scan_id: int) -> int:
    return await session.scalar(
        select(func.count()).select_from(ScanItem).where(ScanItem.scan_id == scan_id)
    )


@pytest.mark.asyncio
class TestUrlChange:
    async def test_a_new_url_rearms_the_baseline(self, client, session, scan):
        with patch("app.web.routes.schedule_scan"):
            resp = await client.post(f"/scanner/{scan.id}", data=form(url=NEW_URL))
        assert resp.status_code == 303
        await session.refresh(scan)
        assert scan.url == NEW_URL
        assert scan.baseline_done is False

    async def test_the_old_products_are_forgotten(self, client, session, scan):
        assert await item_count(session, scan.id) == 3
        with patch("app.web.routes.schedule_scan"):
            await client.post(f"/scanner/{scan.id}", data=form(url=NEW_URL))
        assert await item_count(session, scan.id) == 0

    async def test_other_edits_keep_the_baseline(self, client, session, scan):
        """Clearing the keyword filter must not re-arm — same page, same products."""
        with patch("app.web.routes.schedule_scan"):
            await client.post(f"/scanner/{scan.id}", data=form(keywords=""))
        await session.refresh(scan)
        assert scan.keywords == []
        assert scan.baseline_done is True
        assert await item_count(session, scan.id) == 3

    async def test_a_new_scanner_is_not_treated_as_a_change(self, client, session):
        with patch("app.web.routes.schedule_scan"):
            resp = await client.post("/scanner", data=form(url=NEW_URL))
        assert resp.status_code == 303
        created = (
            (await session.execute(select(ProductScan).where(ProductScan.url == NEW_URL)))
            .scalars()
            .one()
        )
        assert created.baseline_done is False  # first run records silently anyway
