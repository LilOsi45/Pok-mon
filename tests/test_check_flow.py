"""End-to-end check_watch flow with a fake adapter: DB rows, transitions, events."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import EventType, StockCheck, StockStatus, Watch
from app.monitor.base import AdapterError, PageResult, RetailerAdapter, StockResult
from app.monitor.service import check_watch


class FakeAdapter(RetailerAdapter):
    slug = "fake"
    name = "Fake"
    domains = ("example.com",)

    def __init__(self, result: StockResult | Exception):
        super().__init__({})
        self._result = result

    async def fetch(self, url: str) -> PageResult:
        return PageResult(url=url, final_url=url, status_code=200, elapsed_ms=5)

    def parse(self, page: PageResult) -> StockResult:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


async def run_check(session, watch: Watch, result: StockResult | Exception):
    session.add(watch)
    await session.commit()
    with (
        patch("app.monitor.service.resolve_adapter", return_value=FakeAdapter(result)),
        patch("app.monitor.service.dispatch_event", AsyncMock()) as dispatched,
    ):
        check = await check_watch(session, watch)
    return check, dispatched


@pytest.mark.asyncio
async def test_restock_end_to_end(session, watch):
    # baseline: first check sees it OOS
    check, dispatched = await run_check(
        session, watch, StockResult(status=StockStatus.OUT_OF_STOCK, title="P", price=99.0)
    )
    assert check.status == StockStatus.OUT_OF_STOCK
    assert watch.listing_seen is True
    assert dispatched.call_count == 0

    # second check: back in stock -> event dispatched, state updated
    with (
        patch(
            "app.monitor.service.resolve_adapter",
            return_value=FakeAdapter(
                StockResult(status=StockStatus.IN_STOCK, title="P", price=95.0)
            ),
        ),
        patch("app.monitor.service.dispatch_event", AsyncMock()) as dispatched,
    ):
        await check_watch(session, watch)
    assert dispatched.call_count == 1
    event = dispatched.call_args.args[1]
    assert event.type == EventType.BACK_IN_STOCK
    assert event.price == 95.0
    assert "stock:pokemon" in event.routes
    assert watch.last_status == StockStatus.IN_STOCK
    assert watch.last_notified_at is not None
    assert watch.last_in_stock_at is not None

    checks = (await session.execute(select(StockCheck))).scalars().all()
    assert len(checks) == 2


@pytest.mark.asyncio
async def test_adapter_error_recorded_never_raises(session, watch):
    check, dispatched = await run_check(session, watch, AdapterError("shop exploded"))
    assert check.error == "shop exploded"
    assert watch.last_error == "shop exploded"
    assert dispatched.call_count == 0


@pytest.mark.asyncio
async def test_price_title_persisted_on_watch(session, watch):
    await run_check(
        session,
        watch,
        StockResult(
            status=StockStatus.IN_STOCK,
            title="KP11 Display",
            price=149.99,
            image_url="https://img/x.jpg",
            buy_url="https://shop/x",
        ),
    )
    assert watch.last_price == 149.99
    assert watch.last_title == "KP11 Display"
    assert watch.last_image_url == "https://img/x.jpg"
    assert watch.last_buy_url == "https://shop/x"
