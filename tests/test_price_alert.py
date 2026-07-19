"""Price target alerts: crossing fires once, rearms above target, plays nice
with restock events."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.models import EventType, StockStatus, Watch, utcnow
from app.monitor.base import StockResult
from app.monitor.service import check_watch, evaluate_price_target


def result(status: StockStatus, price: float | None) -> StockResult:
    return StockResult(status=status, price=price, title="Produkt")


def armed_watch(watch: Watch, target: float) -> Watch:
    watch.price_target = target
    watch.price_target_hit = False
    watch.listing_seen = True
    watch.last_check_at = utcnow()
    watch.last_status = StockStatus.IN_STOCK
    return watch


class TestEvaluatePriceTarget:
    def test_fires_at_or_below_target(self, watch):
        watch = armed_watch(watch, 130.0)
        event = evaluate_price_target(watch, result(StockStatus.IN_STOCK, 129.99))
        assert event is not None
        assert event.type == EventType.PRICE_DROP
        assert "129.99" in event.message
        assert watch.price_target_hit is True

    def test_does_not_refire_while_below(self, watch):
        watch = armed_watch(watch, 130.0)
        assert evaluate_price_target(watch, result(StockStatus.IN_STOCK, 125.0)) is not None
        assert evaluate_price_target(watch, result(StockStatus.IN_STOCK, 124.0)) is None

    def test_rearms_above_target(self, watch):
        watch = armed_watch(watch, 130.0)
        assert evaluate_price_target(watch, result(StockStatus.IN_STOCK, 125.0)) is not None
        assert evaluate_price_target(watch, result(StockStatus.IN_STOCK, 140.0)) is None
        assert evaluate_price_target(watch, result(StockStatus.IN_STOCK, 128.0)) is not None

    def test_out_of_stock_never_fires(self, watch):
        watch = armed_watch(watch, 130.0)
        assert evaluate_price_target(watch, result(StockStatus.OUT_OF_STOCK, 99.0)) is None

    def test_no_target_no_event(self, watch):
        watch.price_target = None
        assert evaluate_price_target(watch, result(StockStatus.IN_STOCK, 1.0)) is None

    def test_priority_propagates(self, watch):
        watch = armed_watch(watch, 130.0)
        watch.priority = True
        event = evaluate_price_target(watch, result(StockStatus.IN_STOCK, 100.0))
        assert event.priority is True


class FakeAdapter:
    def __init__(self, res: StockResult):
        self.slug = "fake"
        self._res = res

    async def check(self, url: str):
        from app.monitor.base import PageResult

        return PageResult(url=url, final_url=url, status_code=200), self._res


@pytest.mark.asyncio
async def test_restock_event_arms_target_no_double_ping(session, watch):
    """Restock at a price below target: one BACK_IN_STOCK ping, and the next
    check must not follow up with a redundant PRICE_DROP."""
    watch.price_target = 150.0
    watch.listing_seen = True
    watch.last_check_at = utcnow()
    watch.last_status = StockStatus.OUT_OF_STOCK
    session.add(watch)
    await session.commit()

    with (
        patch(
            "app.monitor.service.resolve_adapter",
            return_value=FakeAdapter(result(StockStatus.IN_STOCK, 139.99)),
        ),
        patch("app.monitor.service.dispatch_event", AsyncMock()) as dispatched,
    ):
        await check_watch(session, watch)
        assert dispatched.call_count == 1
        assert dispatched.call_args.args[1].type == EventType.BACK_IN_STOCK
        assert watch.price_target_hit is True

        await check_watch(session, watch)  # still in stock below target
        assert dispatched.call_count == 1  # no second ping
