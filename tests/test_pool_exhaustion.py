"""Regression: slow fetches must not starve the DB connection pool.

A watch held its pooled connection for the whole network call. With dozens of
watches on short intervals and unlocker fetches taking 30 s+, all 15 pooled
connections were busy and the dashboard died with

    sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached

Two defences are asserted here: the connection is released before fetching, and
concurrent scheduled checks are capped.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app import limits
from app.models import Game, ProductScan, StockStatus, Watch
from app.monitor.base import PageResult, StockResult
from app.monitor.service import check_watch, check_watch_by_id


@pytest.fixture(autouse=True)
def _reset_slots():
    limits.reset_for_tests()
    yield
    limits.reset_for_tests()


class SlowAdapter:
    """Reports whether a DB connection was still checked out while fetching."""

    slug = "slow"

    def __init__(self, session, seen: list[bool]):
        self._session = session
        self._seen = seen

    async def check(self, url: str):
        # in_transaction() is True exactly while a connection is held
        self._seen.append(self._session.in_transaction())
        await asyncio.sleep(0)
        page = PageResult(url=url, final_url=url, status_code=200, text="")
        return page, StockResult(status=StockStatus.OUT_OF_STOCK, title="X")


@pytest.mark.asyncio
async def test_connection_is_released_before_the_fetch(session, watch):
    """The whole point: no open transaction while waiting on the network."""
    session.add(watch)
    await session.commit()
    await session.refresh(watch)  # re-open a transaction, as the scheduler does
    assert session.in_transaction()

    held: list[bool] = []
    with (
        patch(
            "app.monitor.service.resolve_adapter",
            return_value=SlowAdapter(session, held),
        ),
        patch("app.monitor.service.dispatch_event", AsyncMock()),
    ):
        await check_watch(session, watch)

    assert held == [False], "a DB connection was still held during the fetch"


@pytest.mark.asyncio
async def test_check_still_records_its_result_after_the_release(session, watch):
    """Releasing the connection must not lose the check row or the status."""
    session.add(watch)
    await session.commit()

    with (
        patch(
            "app.monitor.service.resolve_adapter",
            return_value=SlowAdapter(session, []),
        ),
        patch("app.monitor.service.dispatch_event", AsyncMock()),
    ):
        check = await check_watch(session, watch)

    assert check.id is not None
    assert check.status == StockStatus.OUT_OF_STOCK
    assert watch.last_status == StockStatus.OUT_OF_STOCK
    assert watch.last_check_at is not None


class TestConcurrencyCeiling:
    @pytest.mark.asyncio
    async def test_scheduled_checks_are_capped(self, monkeypatch):
        """Never more than scheduler_max_workers checks in flight at once."""
        import app.config as config

        monkeypatch.setenv("SCHEDULER_MAX_WORKERS", "3")
        config.get_settings.cache_clear()
        limits.reset_for_tests()

        running = 0
        peak = 0

        async def fake_check(_session, _watch):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, _model, _id):
                return Watch(
                    id=_id,
                    game=Game.POKEMON,
                    label="w",
                    url="https://shop.example/x",
                    enabled=True,
                )

        with (
            patch("app.db.get_sessionmaker", lambda: (lambda: FakeSession())),
            patch("app.monitor.service.check_watch", fake_check),
        ):
            await asyncio.gather(*(check_watch_by_id(i) for i in range(12)))

        assert peak <= 3, f"{peak} checks ran at once, limit was 3"
        config.get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_scanner_uses_the_same_ceiling(self, monkeypatch):
        import app.config as config
        from app.scanner import run_scan_by_id

        monkeypatch.setenv("SCHEDULER_MAX_WORKERS", "2")
        config.get_settings.cache_clear()
        limits.reset_for_tests()

        running = 0
        peak = 0

        async def fake_scan(_session, _scan):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1
            return []

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, _model, _id):
                return ProductScan(
                    id=_id,
                    game=Game.POKEMON,
                    label="s",
                    url="https://shop.example/c",
                    keywords=["x"],
                    enabled=True,
                )

        with (
            patch("app.db.get_sessionmaker", lambda: (lambda: FakeSession())),
            patch("app.scanner.run_scan", fake_scan),
        ):
            await asyncio.gather(*(run_scan_by_id(i) for i in range(8)))

        assert peak <= 2, f"{peak} scans ran at once, limit was 2"
        config.get_settings.cache_clear()


def test_engine_pool_has_headroom(monkeypatch):
    """The default 5+10 pool is what broke; assert the raised defaults stick."""
    import app.config as config
    from app import db

    config.get_settings.cache_clear()
    settings = config.get_settings()
    assert settings.db_pool_size >= 20
    assert settings.db_max_overflow >= 20

    db.reset_engine_for_tests()
    engine = db.get_engine()
    # SQLite in-memory/file pools expose the configured size
    assert engine.pool.size() == settings.db_pool_size
    db.reset_engine_for_tests()
