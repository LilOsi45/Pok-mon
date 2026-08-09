"""Deleting five dead watches through a phone dashboard does not get done.

They were spending 2718 requests a day on pages that are gone — about a tenth
of all traffic, and the kind of load that earns rate limits on shops we still
need. Deleting is not reversible, so the confirmation is the whole design: no
word, no deletion.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Game, StockCheck, Watch, utcnow
from app.watch_cleanup import CONFIRM, run


async def _seed(session, *, checks: int = 30, error: str | None = "HTTP 404") -> Watch:
    watch = Watch(
        game=Game.POKEMON,
        label="Pokémon Sammelkarten Erste-Partner",
        url="https://www.einzigundartig.de/pokemon-sammelkarten-erste-partner-set",
        enabled=True,
    )
    session.add(watch)
    await session.commit()
    session.add_all(
        [StockCheck(watch_id=watch.id, checked_at=utcnow(), error=error) for _ in range(checks)]
    )
    await session.commit()
    return watch


@pytest.fixture(autouse=True)
def _use_test_session(session, monkeypatch):
    class Reuse:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("app.watch_cleanup.get_sessionmaker", lambda: lambda: Reuse())


@pytest.mark.asyncio
class TestCleanup:
    async def test_without_the_word_nothing_is_deleted(self, session):
        watch = await _seed(session)

        text = await run(confirmed=False)

        assert CONFIRM in text
        assert "Nichts gelöscht" in text
        assert await session.get(Watch, watch.id) is not None

    async def test_the_listing_names_the_id_and_the_full_url(self, session):
        watch = await _seed(session)

        text = await run(confirmed=False)

        assert str(watch.id) in text
        assert watch.url in text

    async def test_with_the_word_they_go(self, session):
        watch = await _seed(session)

        text = await run(confirmed=True)

        assert "1 Watches gelöscht" in text
        assert (await session.execute(select(Watch).where(Watch.id == watch.id))).first() is None

    async def test_a_healthy_watch_is_never_touched(self, session):
        watch = await _seed(session, error=None)

        text = await run(confirmed=True)

        assert "Keine toten Links" in text
        assert await session.get(Watch, watch.id) is not None

    async def test_a_rate_limited_watch_is_not_a_dead_link(self, session):
        """A shop refusing us is not a wrong address, and deleting is final."""
        watch = await _seed(session, error="RateLimited: shop.de antwortet 429 (direkt)")

        await run(confirmed=True)

        assert await session.get(Watch, watch.id) is not None

    async def test_it_says_to_restart_afterwards(self, session):
        await _seed(session)
        assert "restart" in await run(confirmed=True)
