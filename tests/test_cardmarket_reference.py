"""Cardmarket reference price: parsing, caching, embed rendering, resilience."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app import cardmarket
from app.events import Event
from app.models import EventType, Game, StockStatus, Watch
from app.monitor.base import StockResult
from app.monitor.service import attach_cardmarket
from app.notify.discord import build_embed

PRODUCT = {
    "product": {
        "idProduct": 771234,
        "enName": "Konoha Shido Booster Display (2nd Edition)",
        "website": "/en/Naruto/Products/Booster-Displays/Konoha-Shido",
        "priceGuide": {"LOW": 79.0, "TREND": 89.9, "AVG": 92.0},
    }
}


@pytest.fixture(autouse=True)
def _clear_cache():
    cardmarket.clear_cache()
    yield
    cardmarket.clear_cache()


def _creds():
    return patch.object(cardmarket, "_credentials", lambda: ("a", "b", "c", "d"))


class TestPriceReference:
    @pytest.mark.asyncio
    async def test_parses_trend_low_and_url(self):
        with _creds(), patch.object(cardmarket, "_request", AsyncMock(return_value=PRODUCT)):
            ref = await cardmarket.price_reference(771234)
        assert ref is not None
        assert ref.trend == 89.9
        assert ref.low == 79.0
        assert ref.url == (
            "https://www.cardmarket.com/en/Naruto/Products/Booster-Displays/Konoha-Shido"
        )
        assert ref.has_price

    @pytest.mark.asyncio
    async def test_falls_back_through_guide_keys(self):
        data = {"product": {"enName": "X", "priceGuide": {"AVG7": 12.5}}}
        with _creds(), patch.object(cardmarket, "_request", AsyncMock(return_value=data)):
            ref = await cardmarket.price_reference(1)
        assert ref is not None and ref.trend == 12.5 and ref.low is None

    @pytest.mark.asyncio
    async def test_second_call_is_served_from_cache(self):
        request = AsyncMock(return_value=PRODUCT)
        with _creds(), patch.object(cardmarket, "_request", request):
            await cardmarket.price_reference(771234)
            await cardmarket.price_reference(771234)
        assert request.await_count == 1  # the 5000/day cap is precious

    @pytest.mark.asyncio
    async def test_no_credentials_returns_none_without_calling_out(self):
        with patch.object(cardmarket, "_credentials", lambda: None):
            assert await cardmarket.price_reference(771234) is None

    @pytest.mark.asyncio
    async def test_garbage_response_is_none_not_a_crash(self):
        with _creds(), patch.object(cardmarket, "_request", AsyncMock(return_value={"oops": 1})):
            assert await cardmarket.price_reference(771234) is None


class TestAttachToEvent:
    def _watch(self, cardmarket_id: int | None) -> Watch:
        return Watch(
            id=1,
            game=Game.POKEMON,
            label="Display",
            url="https://shop.example/p",
            cardmarket_id=cardmarket_id,
        )

    def _event(self) -> Event:
        return Event(type=EventType.BACK_IN_STOCK, game=Game.POKEMON, title="t", price=74.99)

    @pytest.mark.asyncio
    async def test_fills_the_event(self):
        event = self._event()
        ref = cardmarket.PriceReference(771234, "X", trend=89.9, low=79.0, url="https://cm/x")
        with patch("app.cardmarket.price_reference", AsyncMock(return_value=ref)):
            await attach_cardmarket(self._watch(771234), event)
        assert event.cardmarket_trend == 89.9
        assert event.cardmarket_url == "https://cm/x"

    @pytest.mark.asyncio
    async def test_watch_without_id_makes_no_request(self):
        lookup = AsyncMock()
        with patch("app.cardmarket.price_reference", lookup):
            await attach_cardmarket(self._watch(None), self._event())
        lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lookup_failure_never_breaks_the_alert(self):
        event = self._event()
        boom = AsyncMock(side_effect=RuntimeError("cardmarket down"))
        with patch("app.cardmarket.price_reference", boom):
            await attach_cardmarket(self._watch(771234), event)  # must not raise
        assert event.cardmarket_trend is None


class TestEmbed:
    def _event(self, **kw) -> Event:
        base = dict(
            type=EventType.BACK_IN_STOCK,
            game=Game.POKEMON,
            title="Back in stock: Display",
            url="https://shop.example/p",
        )
        return Event(**{**base, **kw})

    def _field(self, embed: dict, name: str) -> str | None:
        for field in embed["fields"]:
            if field["name"] == name:
                return field["value"]
        return None

    def test_shows_trend_low_and_savings(self):
        embed = build_embed(
            self._event(price=74.99, cardmarket_trend=89.9, cardmarket_low=79.0)
        )
        value = self._field(embed, "Cardmarket")
        assert value is not None
        assert "89.90" in value and "79.00" in value
        assert "🟢" in value and "14.91" in value

    def test_marks_an_overpriced_shop(self):
        embed = build_embed(self._event(price=109.0, cardmarket_trend=89.9))
        value = self._field(embed, "Cardmarket")
        assert value and "🔴" in value and "19.10" in value

    def test_link_joins_the_buy_row(self):
        embed = build_embed(
            self._event(price=74.99, cardmarket_trend=89.9, cardmarket_url="https://cm/x")
        )
        buy = self._field(embed, "Kaufen")
        assert buy and "[📊 Cardmarket](https://cm/x)" in buy

    def test_no_reference_means_no_field(self):
        embed = build_embed(self._event(price=74.99))
        assert self._field(embed, "Cardmarket") is None

    def test_cent_level_difference_is_not_flagged(self):
        embed = build_embed(self._event(price=89.7, cardmarket_trend=89.9))
        value = self._field(embed, "Cardmarket")
        assert value and "🟢" not in value and "🔴" not in value


class TestFullCheckFlow:
    @pytest.mark.asyncio
    async def test_restock_ping_carries_the_reference(self, session, watch):
        from app.monitor.service import check_watch

        watch.cardmarket_id = 771234
        watch.listing_seen = True
        watch.last_status = StockStatus.OUT_OF_STOCK
        from app.models import utcnow

        watch.last_check_at = utcnow()
        session.add(watch)
        await session.commit()

        class Fake:
            slug = "fake"

            async def check(self, url):
                from app.monitor.base import PageResult

                page = PageResult(url=url, final_url=url, status_code=200, text="")
                return page, StockResult(status=StockStatus.IN_STOCK, price=74.99, title="Display")

        ref = cardmarket.PriceReference(771234, "X", trend=89.9, low=79.0, url="https://cm/x")
        with (
            patch("app.monitor.service.resolve_adapter", return_value=Fake()),
            patch("app.cardmarket.price_reference", AsyncMock(return_value=ref)),
            patch("app.monitor.service.dispatch_event", AsyncMock()) as dispatched,
        ):
            await check_watch(session, watch)

        assert dispatched.call_count == 1
        event = dispatched.call_args.args[1]
        assert event.cardmarket_trend == 89.9
        assert event.cardmarket_url == "https://cm/x"


class TestManualReference:
    """Cardmarket stopped granting API access — the manual path must carry it."""

    def _watch(self, **kw) -> Watch:
        base = dict(id=1, game=Game.POKEMON, label="Display", url="https://shop.example/p")
        return Watch(**{**base, **kw})

    def _event(self) -> Event:
        return Event(type=EventType.BACK_IN_STOCK, game=Game.POKEMON, title="t", price=74.99)

    @pytest.mark.asyncio
    async def test_manual_price_and_link_are_used(self):
        event = self._event()
        watch = self._watch(reference_price=89.9, reference_url="https://www.cardmarket.com/de/x")
        await attach_cardmarket(watch, event)
        assert event.cardmarket_trend == 89.9
        assert event.cardmarket_url == "https://www.cardmarket.com/de/x"
        assert event.cardmarket_live is False

    @pytest.mark.asyncio
    async def test_manual_only_a_link_still_shows_the_button(self):
        event = self._event()
        await attach_cardmarket(self._watch(reference_url="https://cm/x"), event)
        assert event.cardmarket_url == "https://cm/x"
        assert event.cardmarket_trend is None

    @pytest.mark.asyncio
    async def test_live_api_price_wins_over_the_manual_one(self):
        event = self._event()
        ref = cardmarket.PriceReference(7, "X", trend=95.0, low=88.0, url="https://cm/live")
        watch = self._watch(cardmarket_id=7, reference_price=89.9, reference_url="https://cm/manual")
        with patch("app.cardmarket.price_reference", AsyncMock(return_value=ref)):
            await attach_cardmarket(watch, event)
        assert event.cardmarket_trend == 95.0
        assert event.cardmarket_live is True

    @pytest.mark.asyncio
    async def test_api_denied_falls_back_to_the_manual_price(self):
        # exactly today's situation: credentials rejected -> price_reference None
        event = self._event()
        watch = self._watch(cardmarket_id=7, reference_price=89.9, reference_url="https://cm/x")
        with patch("app.cardmarket.price_reference", AsyncMock(return_value=None)):
            await attach_cardmarket(watch, event)
        assert event.cardmarket_trend == 89.9
        assert event.cardmarket_live is False

    def test_manual_price_renders_without_the_trend_label(self):
        embed = build_embed(
            Event(
                type=EventType.BACK_IN_STOCK,
                game=Game.POKEMON,
                title="t",
                url="https://shop.example/p",
                price=74.99,
                cardmarket_trend=89.9,
                cardmarket_live=False,
            )
        )
        value = next(f["value"] for f in embed["fields"] if f["name"] == "Cardmarket")
        assert "Trend" not in value
        assert "89.90" in value and "🟢" in value
