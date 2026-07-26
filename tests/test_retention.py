"""History retention — and the one table that must never be pruned."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    EventType,
    Game,
    Notification,
    ProductScan,
    ScanItem,
    StockCheck,
    StockStatus,
    utcnow,
)
from app.retention import prune_old_rows, row_counts


async def _seed(session, watch_id: int) -> None:
    now = utcnow()
    for age_days in (0, 5, 13, 15, 60):
        session.add(
            StockCheck(
                watch_id=watch_id,
                status=StockStatus.OUT_OF_STOCK,
                checked_at=now - timedelta(days=age_days),
            )
        )
    for age_days in (0, 30, 89, 91, 400):
        session.add(
            Notification(
                event_type=EventType.BACK_IN_STOCK,
                notifier="tcg-check",
                notifier_type="discord",
                title="t",
                success=True,
                created_at=now - timedelta(days=age_days),
            )
        )
    await session.commit()


@pytest.mark.asyncio
async def test_prunes_only_rows_past_the_cutoff(session, watch):
    session.add(watch)
    await session.commit()
    await _seed(session, watch.id)

    result = await prune_old_rows(session)

    # defaults: checks 14 days, notifications 90 days
    assert result.checks_deleted == 2  # 15 and 60 days old
    assert result.notifications_deleted == 2  # 91 and 400 days old
    assert (await session.scalar(select(func.count(StockCheck.id)))) == 3
    assert (await session.scalar(select(func.count(Notification.id)))) == 3


@pytest.mark.asyncio
async def test_scanner_memory_is_never_pruned(session):
    """scan_items is the dedupe memory — pruning it would re-alert everything."""
    scan = ProductScan(
        game=Game.POKEMON, label="s", url="https://shop.example/c", keywords=["x"]
    )
    session.add(scan)
    await session.commit()
    old = utcnow() - timedelta(days=900)
    session.add(
        ScanItem(
            scan_id=scan.id,
            url="https://shop.example/p",
            url_key="https://shop.example/p",
            title="Uraltes Produkt",
            first_seen=old,
            notified=True,
        )
    )
    await session.commit()

    await prune_old_rows(session)

    assert (await session.scalar(select(func.count(ScanItem.id)))) == 1


@pytest.mark.asyncio
async def test_nothing_to_do_is_not_an_error(session):
    result = await prune_old_rows(session)
    assert result.total == 0


@pytest.mark.asyncio
async def test_retention_is_configurable(session, watch, monkeypatch):
    import app.config as config

    session.add(watch)
    await session.commit()
    await _seed(session, watch.id)

    monkeypatch.setenv("CHECK_RETENTION_DAYS", "3")
    config.get_settings.cache_clear()
    try:
        result = await prune_old_rows(session)
        assert result.checks_deleted == 4  # everything but the one from today
    finally:
        config.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_row_counts_reports_both_tables(session, watch):
    session.add(watch)
    await session.commit()
    await _seed(session, watch.id)
    counts = await row_counts(session)
    assert counts == {"stock_checks": 5, "notifications": 5}


@pytest.mark.asyncio
async def test_prune_job_runs_standalone(monkeypatch):
    """The scheduler calls this with no session of its own."""
    from unittest.mock import AsyncMock, patch

    from app.retention import prune_job

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    called = AsyncMock()
    with (
        patch("app.db.get_sessionmaker", lambda: (lambda: FakeSession())),
        patch("app.retention.prune_old_rows", called),
    ):
        await prune_job()
    called.assert_awaited_once()


def test_retention_job_is_scheduled():
    """A tool nobody runs is no tool — assert it is wired into the scheduler."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "scheduler.py").read_text()
    assert "prune_job" in source
    assert "system:retention" in source
