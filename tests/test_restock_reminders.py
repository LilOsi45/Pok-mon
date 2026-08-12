"""A restock was announced exactly once, and then never again.

In a busy channel that single message scrolls away within minutes, so the drop
is missed even though the tracker saw it — which is the whole failure this
project exists to prevent. While the product stays in stock the alert repeats,
one cooldown apart, a limited number of times: plenty of watched products are
simply always available, and repeating those forever is how an alert channel
stops being read.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import app.config as config
from app.models import EventType, StockStatus, utcnow
from app.monitor.base import StockResult
from app.monitor.service import _apply_result, _build_event, evaluate_transition

IN_STOCK = StockResult(status=StockStatus.IN_STOCK, title="Top-Trainer-Box", price=59.99)
SOLD_OUT = StockResult(status=StockStatus.OUT_OF_STOCK, title="Top-Trainer-Box")


@pytest.fixture(autouse=True)
def _reminders(monkeypatch):
    monkeypatch.setenv("RESTOCK_REMINDERS", "2")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def in_stock_watch(watch, *, ago_minutes: float, sent: int = 0):
    watch.listing_seen = True
    watch.last_status = StockStatus.IN_STOCK
    watch.cooldown_seconds = 1800
    watch.reminders_sent = sent
    watch.last_notified_at = utcnow() - timedelta(minutes=ago_minutes)
    return watch


class TestReminders:
    def test_a_reminder_follows_one_cooldown_later(self, watch):
        w = in_stock_watch(watch, ago_minutes=31)
        assert evaluate_transition(w, IN_STOCK) is EventType.BACK_IN_STOCK

    def test_nothing_before_the_cooldown_is_up(self, watch):
        w = in_stock_watch(watch, ago_minutes=5)
        assert evaluate_transition(w, IN_STOCK) is None

    def test_it_stops_after_the_configured_number(self, watch):
        w = in_stock_watch(watch, ago_minutes=31, sent=2)
        assert evaluate_transition(w, IN_STOCK) is None

    def test_zero_disables_them(self, watch, monkeypatch):
        monkeypatch.setenv("RESTOCK_REMINDERS", "0")
        config.get_settings.cache_clear()
        w = in_stock_watch(watch, ago_minutes=999)
        assert evaluate_transition(w, IN_STOCK) is None

    def test_the_first_restock_is_still_a_restock(self, watch):
        watch.listing_seen = True
        watch.last_status = StockStatus.OUT_OF_STOCK
        watch.last_notified_at = None
        assert evaluate_transition(watch, IN_STOCK) is EventType.BACK_IN_STOCK


class TestTheStreakResets:
    def test_going_out_of_stock_clears_the_counter(self, watch):
        watch.reminders_sent = 2
        _apply_result(watch, SOLD_OUT)
        assert watch.reminders_sent == 0

    def test_an_unreadable_page_does_not_re_arm_them(self, watch):
        """A partial page must not hand a product a fresh set of reminders."""
        watch.reminders_sent = 2
        _apply_result(watch, StockResult(status=StockStatus.UNKNOWN, readable=False))
        assert watch.reminders_sent == 2

    def test_staying_in_stock_keeps_the_counter(self, watch):
        watch.reminders_sent = 1
        _apply_result(watch, IN_STOCK)
        assert watch.reminders_sent == 1


class TestWording:
    def test_a_reminder_reads_as_a_repeat(self, watch):
        watch.label = "Pokémon 30 Jahre TTB"
        event = _build_event(watch, IN_STOCK, EventType.BACK_IN_STOCK, reminder=True)
        assert "Immer noch da" in event.title

    def test_the_first_announcement_is_unchanged(self, watch):
        watch.label = "Pokémon 30 Jahre TTB"
        event = _build_event(watch, IN_STOCK, EventType.BACK_IN_STOCK)
        assert event.title.startswith("Back in stock")


class TestAnUnflushedWatch:
    """A Watch built in memory has no counter until it is flushed, and
    comparing None to an int raises instead of simply not reminding."""

    def test_it_does_not_crash(self, watch):
        watch.listing_seen = True
        watch.last_status = StockStatus.IN_STOCK
        watch.reminders_sent = None
        watch.last_notified_at = utcnow() - timedelta(hours=5)

        assert evaluate_transition(watch, IN_STOCK) is EventType.BACK_IN_STOCK
