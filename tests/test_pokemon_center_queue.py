"""Pokémon Center queue alarm: signal detection + end-to-end transition."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.models import EventType, StockStatus
from app.monitor.adapters.pokemon_center_queue import PokemonCenterQueueAdapter
from app.monitor.base import PageResult
from app.monitor.registry import resolve_adapter
from app.monitor.service import check_watch

URL = "https://www.pokemoncenter.com/de-de"


def page(
    status: int = 200,
    text: str = "<html><body>Welcome to the Pokémon Center!</body></html>",
    location: str | None = None,
    final_url: str = URL,
) -> PageResult:
    headers = {"location": location} if location else {}
    return PageResult(url=URL, final_url=final_url, status_code=status, text=text, headers=headers)


class TestDetection:
    def test_redirect_to_queue_means_live(self):
        result = PokemonCenterQueueAdapter().parse(
            page(status=302, location="https://pokemoncenter.queue-it.net/?c=pokemoncenter&e=drop1")
        )
        assert result.status == StockStatus.IN_STOCK
        assert result.alert_title and "OFFEN" in result.alert_title

    def test_followed_redirect_final_url(self):
        # Playwright follows redirects; the final URL is the signal then
        result = PokemonCenterQueueAdapter().parse(
            page(final_url="https://pokemoncenter.queue-it.net/?c=pokemoncenter")
        )
        assert result.status == StockStatus.IN_STOCK

    def test_queue_page_body_phrases(self):
        result = PokemonCenterQueueAdapter().parse(
            page(
                status=503,
                text="<html><body>You are now in line. Estimated wait: 20 min</body></html>",
            )
        )
        assert result.status == StockStatus.IN_STOCK

    def test_normal_page_is_idle_not_unknown(self):
        result = PokemonCenterQueueAdapter().parse(page())
        assert result.status == StockStatus.OUT_OF_STOCK

    def test_generic_queueit_script_tag_does_not_trigger(self):
        html = '<html><head><script src="https://pokemoncenter.queue-it.net/queue-client.js"></script></head><body>Shop</body></html>'
        # script tag is in body text but host marker only counts for redirects/final URL
        result = PokemonCenterQueueAdapter().parse(page(text=html))
        assert result.status == StockStatus.OUT_OF_STOCK

    def test_challenge_403_is_idle_baseline(self):
        # PC serves a permanent JS challenge (403) at rest → idle, no alert
        result = PokemonCenterQueueAdapter().parse(page(status=403, text="cmsg challenge"))
        assert result.status == StockStatus.OUT_OF_STOCK
        assert "idle" in result.note

    def test_503_overload_signals_activity(self):
        # the wall going up during a drop shows as an overload → alert
        result = PokemonCenterQueueAdapter().parse(page(status=503, text="<html>busy</html>"))
        assert result.status == StockStatus.IN_STOCK
        assert result.alert_title and "Anti-Bot" in result.alert_title

    def test_429_rate_limit_signals_activity(self):
        # anti-bot throttling us is the same story as an overload
        result = PokemonCenterQueueAdapter().parse(page(status=429, text="too many requests"))
        assert result.status == StockStatus.IN_STOCK
        assert result.alert_title and "Rate-Limit" in result.alert_title

    @pytest.mark.parametrize("status", [500, 520, 521, 522, 525])
    def test_any_5xx_signals_activity(self, status: int):
        # Cloudflare's 52x edge codes appear when the origin/wall is struggling
        result = PokemonCenterQueueAdapter().parse(page(status=status, text="<html>err</html>"))
        assert result.status == StockStatus.IN_STOCK
        assert result.alert_title and "Anti-Bot" in result.alert_title

    def test_404_is_idle_not_an_alert(self):
        # a wrong watch URL must not masquerade as drop activity
        result = PokemonCenterQueueAdapter().parse(page(status=404, text="not found"))
        assert result.status == StockStatus.OUT_OF_STOCK
        assert "idle" in result.note

    def test_not_domain_resolved_but_slug_selectable(self):
        # plain pokemoncenter.com watches keep the product stub…
        assert (
            resolve_adapter("https://www.pokemoncenter.com/de-de/p/x").slug == "pokemon_center_eu"
        )
        # …the queue adapter is chosen explicitly
        assert resolve_adapter(URL, slug="pokemon_center_queue").slug == "pokemon_center_queue"


class QueueFake(PokemonCenterQueueAdapter):
    def __init__(self, result_page: PageResult):
        super().__init__({})
        self._page = result_page

    async def fetch(self, url: str) -> PageResult:
        return self._page


@pytest.mark.asyncio
async def test_queue_open_transition_pings(session, watch):
    watch.label = "Pokémon Center Queue"
    watch.url = URL
    watch.adapter = "pokemon_center_queue"
    session.add(watch)
    await session.commit()

    # 1st check: no queue -> silent baseline (OUT_OF_STOCK)
    with (
        patch("app.monitor.service.resolve_adapter", return_value=QueueFake(page())),
        patch("app.monitor.service.dispatch_event", AsyncMock()) as dispatched,
    ):
        await check_watch(session, watch)
    assert watch.last_status == StockStatus.OUT_OF_STOCK
    assert dispatched.call_count == 0

    # 2nd check: queue opens -> one ping with the custom title
    live = page(status=302, location="https://pokemoncenter.queue-it.net/?c=pokemoncenter")
    with (
        patch("app.monitor.service.resolve_adapter", return_value=QueueFake(live)),
        patch("app.monitor.service.dispatch_event", AsyncMock()) as dispatched,
    ):
        await check_watch(session, watch)
    assert dispatched.call_count == 1
    event = dispatched.call_args.args[1]
    assert event.type == EventType.BACK_IN_STOCK
    assert "Queue ist OFFEN" in event.title
    assert "Pokémon Center Queue" in event.title

    # 3rd check: queue still open -> no repeat ping
    with (
        patch("app.monitor.service.resolve_adapter", return_value=QueueFake(live)),
        patch("app.monitor.service.dispatch_event", AsyncMock()) as dispatched,
    ):
        await check_watch(session, watch)
    assert dispatched.call_count == 0


class TestWatchDebugSignals:
    """The diagnostic must surface exactly the signals the adapter alerts on."""

    def test_reports_a_queue_redirect(self):
        from app.watch_debug import _queue_signals

        hits = _queue_signals(
            page(status=302, location="https://pokemoncenter.queue-it.net/?c=pokemoncenter")
        )
        assert any("Weiterleitung" in h for h in hits)

    def test_reports_a_waiting_room_phrase(self):
        from app.watch_debug import _queue_signals

        hits = _queue_signals(page(text="<html>You are now in line</html>"))
        assert any("you are now in line" in h for h in hits)

    def test_quiet_page_has_no_signals(self):
        from app.watch_debug import _queue_signals

        assert _queue_signals(page(status=403, text="cmsg challenge")) == []


@pytest.mark.asyncio
class TestFetcherChoice:
    """Measured: only the unlocker reaches the site, so it must be selectable."""

    async def _fetch_with(self, detection: dict):
        from app.monitor import fetchers

        adapter = PokemonCenterQueueAdapter(detection)
        calls: list[str] = []

        async def record(name):
            async def _inner(url, **_kw):
                calls.append(name)
                return page()

            return _inner

        with (
            patch.object(fetchers, "fetch_scraperapi", await record("brightdata")),
            patch.object(fetchers, "fetch_playwright", await record("playwright")),
        ):
            await adapter.fetch(URL)
        return calls

    async def test_brightdata_is_selectable(self):
        assert await self._fetch_with({"fetcher": "brightdata"}) == ["brightdata"]

    async def test_playwright_still_selectable(self):
        assert await self._fetch_with({"fetcher": "playwright"}) == ["playwright"]


def test_unlocker_sees_the_waiting_room_through_the_body():
    """Via the unlocker there is no redirect to inspect — the body must carry it."""
    result = PokemonCenterQueueAdapter({"fetcher": "brightdata"}).parse(
        page(status=200, text="<html><body>You are now in line. Estimated wait 12 min</body></html>")
    )
    assert result.status == StockStatus.IN_STOCK
    assert result.alert_title and "OFFEN" in result.alert_title
