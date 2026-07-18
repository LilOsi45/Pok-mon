"""Transition logic: NEW_LISTING / BACK_IN_STOCK / cooldown suppression."""

from __future__ import annotations

from datetime import timedelta

from app.models import EventType, StockStatus, Watch, utcnow
from app.monitor.base import StockResult
from app.monitor.service import evaluate_transition


def result(status: StockStatus, listed: bool = True) -> StockResult:
    return StockResult(status=status, listed=listed, price=99.99, title="Product")


class TestFirstCheck:
    def test_first_check_in_stock_is_silent_baseline(self, watch: Watch):
        assert evaluate_transition(watch, result(StockStatus.IN_STOCK)) is None

    def test_first_check_with_optin_fires_new_listing(self, watch: Watch):
        watch.notify_on_first_seen = True
        assert evaluate_transition(watch, result(StockStatus.IN_STOCK)) == EventType.NEW_LISTING

    def test_first_check_not_listed_no_event(self, watch: Watch):
        assert evaluate_transition(watch, result(StockStatus.UNKNOWN, listed=False)) is None


class TestNewListing:
    def test_unlisted_becomes_listed(self, watch: Watch):
        # watch was checked before but the product was never listed (e.g. 404)
        watch.last_check_at = utcnow()
        watch.listing_seen = False
        watch.last_status = StockStatus.UNKNOWN
        assert evaluate_transition(watch, result(StockStatus.IN_STOCK)) == EventType.NEW_LISTING

    def test_preorder_listing_counts_even_if_oos(self, watch: Watch):
        watch.last_check_at = utcnow()
        watch.listing_seen = False
        assert evaluate_transition(watch, result(StockStatus.OUT_OF_STOCK)) == EventType.NEW_LISTING


class TestBackInStock:
    def _seen_watch(self, watch: Watch, last_status: StockStatus) -> Watch:
        watch.last_check_at = utcnow()
        watch.listing_seen = True
        watch.last_status = last_status
        return watch

    def test_oos_to_instock_fires(self, watch: Watch):
        watch = self._seen_watch(watch, StockStatus.OUT_OF_STOCK)
        assert evaluate_transition(watch, result(StockStatus.IN_STOCK)) == EventType.BACK_IN_STOCK

    def test_unknown_to_instock_fires(self, watch: Watch):
        watch = self._seen_watch(watch, StockStatus.UNKNOWN)
        assert evaluate_transition(watch, result(StockStatus.IN_STOCK)) == EventType.BACK_IN_STOCK

    def test_instock_to_instock_is_quiet(self, watch: Watch):
        watch = self._seen_watch(watch, StockStatus.IN_STOCK)
        assert evaluate_transition(watch, result(StockStatus.IN_STOCK)) is None

    def test_instock_to_oos_is_quiet(self, watch: Watch):
        watch = self._seen_watch(watch, StockStatus.IN_STOCK)
        assert evaluate_transition(watch, result(StockStatus.OUT_OF_STOCK)) is None

    def test_cooldown_suppresses(self, watch: Watch):
        watch = self._seen_watch(watch, StockStatus.OUT_OF_STOCK)
        watch.last_notified_at = utcnow() - timedelta(seconds=60)  # within 1800s cooldown
        assert evaluate_transition(watch, result(StockStatus.IN_STOCK)) is None

    def test_cooldown_expired_fires_again(self, watch: Watch):
        watch = self._seen_watch(watch, StockStatus.OUT_OF_STOCK)
        watch.last_notified_at = utcnow() - timedelta(seconds=watch.cooldown_seconds + 5)
        assert evaluate_transition(watch, result(StockStatus.IN_STOCK)) == EventType.BACK_IN_STOCK
