"""A page we could not read must not arm a restock ping.

Measured on elbenwald.de: the same product URL returned 533415 characters with
a buy button and a price on one fetch, and 40459 characters with nothing on the
next. Reading the short one as UNKNOWN made the following complete read look
like UNKNOWN -> IN_STOCK — a restock alert for a product whose availability had
never changed. That is why the shop pinged constantly while nothing could
actually be bought.
"""

from __future__ import annotations

import pytest

from app.models import EventType, StockStatus
from app.monitor.base import StockResult
from app.monitor.detection import detect_stock
from app.monitor.service import _apply_result, evaluate_transition

FULL = """
<html><body><h1>Winnie Puuh Nachtlicht</h1>
  <span>29,95 €</span>
  <button class="add-to-cart">In den Warenkorb</button>
</body></html>
"""
PARTIAL = "<html><body><h1>Winnie Puuh Nachtlicht</h1><nav>Suche</nav>"


class TestDetectionMarksIt:
    def test_a_page_with_no_signal_is_marked_unreadable(self):
        result = detect_stock(PARTIAL)
        assert result.status is StockStatus.UNKNOWN
        assert result.readable is False

    def test_a_complete_page_is_readable(self):
        result = detect_stock(FULL)
        assert result.status is StockStatus.IN_STOCK
        assert result.readable is True

    def test_a_sold_out_page_is_readable(self):
        html = FULL.replace("In den Warenkorb", "Leider ausverkauft")
        result = detect_stock(html)
        assert result.readable is True


@pytest.mark.asyncio
class TestTheBaselineSurvives:
    async def test_an_unreadable_check_keeps_the_last_status(self, watch):
        watch.last_status = StockStatus.IN_STOCK
        watch.listing_seen = True

        _apply_result(watch, detect_stock(PARTIAL))

        assert watch.last_status is StockStatus.IN_STOCK
        assert watch.last_error and "nicht lesbar" in watch.last_error

    async def test_no_restock_ping_after_a_partial_page(self, watch):
        """The whole point: partial page, then the complete one, no ping."""
        watch.last_status = StockStatus.IN_STOCK
        watch.listing_seen = True

        _apply_result(watch, detect_stock(PARTIAL))
        assert evaluate_transition(watch, detect_stock(FULL)) is None

    async def test_a_real_restock_still_pings(self, watch):
        watch.last_status = StockStatus.OUT_OF_STOCK
        watch.listing_seen = True
        watch.last_notified_at = None

        assert evaluate_transition(watch, detect_stock(FULL)) is EventType.BACK_IN_STOCK

    async def test_a_readable_check_clears_the_error(self, watch):
        watch.last_error = "Seite nicht lesbar (kein Signal) — Lagerstand unverändert übernommen"

        _apply_result(watch, detect_stock(FULL))

        assert watch.last_error is None
        assert watch.last_status is StockStatus.IN_STOCK

    async def test_an_explicit_unknown_from_an_adapter_still_counts(self, watch):
        """A 404 is a real answer — the page told us the product is not there."""
        watch.last_status = StockStatus.IN_STOCK

        _apply_result(watch, StockResult(status=StockStatus.UNKNOWN, listed=False, note="HTTP 404"))

        assert watch.last_status is StockStatus.UNKNOWN


@pytest.mark.asyncio
class TestOneMoreAttempt:
    """89 % of elbenwald.de's checks landed on a partial page.

    The same URL served 533415 characters with a buy button and a price on one
    fetch and 40459 with none of it on the next, so the reading that gets
    thrown away is worth one more request — and only on a check that failed.
    """

    async def _adapter(self, monkeypatch, pages):
        from app.monitor.adapters.generic import GenericAdapter
        from app.monitor.base import PageResult

        monkeypatch.setattr("app.monitor.adapters.generic.RETRY_UNREADABLE_SECONDS", 0)
        monkeypatch.setattr(
            "app.monitor.adapters.generic.get_settings",
            lambda: type("S", (), {"use_shop_catalog": False})(),
            raising=False,
        )
        calls = {"n": 0}

        async def fake_fetch(_self, url):
            html = pages[min(calls["n"], len(pages) - 1)]
            calls["n"] += 1
            return PageResult(url=url, final_url=url, status_code=200, text=html)

        monkeypatch.setattr(GenericAdapter, "fetch", fake_fetch)
        return GenericAdapter(), calls

    async def test_a_partial_page_is_retried_and_the_good_one_wins(self, monkeypatch):
        adapter, calls = await self._adapter(monkeypatch, [PARTIAL, FULL])

        _page, result = await adapter.check("https://www.elbenwald.de/x")

        assert calls["n"] == 2
        assert result.status is StockStatus.IN_STOCK

    async def test_a_readable_page_costs_one_request(self, monkeypatch):
        adapter, calls = await self._adapter(monkeypatch, [FULL])

        await adapter.check("https://www.elbenwald.de/x")

        assert calls["n"] == 1

    async def test_two_partial_pages_stay_unreadable(self, monkeypatch):
        adapter, calls = await self._adapter(monkeypatch, [PARTIAL, PARTIAL])

        _page, result = await adapter.check("https://www.elbenwald.de/x")

        assert calls["n"] == 2
        assert result.readable is False
