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

# The bot interstitial as measured on the live server (1055 chars direct, 1565
# via Playwright), shortened. Its fingerprint matters: the adapter used to treat
# *any* small page as this challenge, which meant a waiting room in wording we
# do not have was filed as "just the bot wall" and never alerted. Tests that
# fake the challenge must therefore look like the real thing.
CHALLENGE_HTML = (
    '<html style="height:100%"><head><META NAME="ROBOTS" CONTENT="NOINDEX, NOFOLLOW">'
    '<script src="/vice-come-Soldenyson" async=""></script>'
    "<style>#cmsg{animation: A 1.5s;}</style></head><body></body></html>"
)


def page(
    status: int = 200,
    # A store marker is what tells the real page apart from the ~1 KB bot
    # interstitial that also comes back as HTTP 200.
    text: str = '<html><body>Welcome!<a href="/de-de/product/box">Box</a></body></html>',
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
        html = (
            '<html><head><script src="https://pokemoncenter.queue-it.net/queue-client.js">'
            '</script></head><body>Shop<a href="/de-de/product/box">Box</a></body></html>'
        )
        # script tag is in body text but host marker only counts for redirects/final URL
        result = PokemonCenterQueueAdapter().parse(page(text=html))
        assert result.status == StockStatus.OUT_OF_STOCK

    def test_a_challenge_403_is_blind_not_idle(self):
        """Corrected: a challenge means we cannot see the store, so not "no queue".

        Reporting the challenge as idle made a watch that can never observe a
        queue look healthy. It is UNKNOWN — the alert on 429/5xx still fires,
        because that check runs first.
        """
        result = PokemonCenterQueueAdapter().parse(page(status=403, text=CHALLENGE_HTML))
        assert result.status == StockStatus.UNKNOWN
        assert "Bot-Prüfseite" in result.note

    def test_503_overload_signals_activity(self):
        # the wall going up during a drop shows as an overload → alert
        result = PokemonCenterQueueAdapter().parse(page(status=503, text="<html>busy</html>"))
        assert result.status == StockStatus.IN_STOCK
        assert result.alert_title and "viel Betrieb" in result.alert_title

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
        assert result.alert_title and "viel Betrieb" in result.alert_title

    def test_404_is_never_an_alert(self):
        """A wrong watch URL must not masquerade as drop activity."""
        result = PokemonCenterQueueAdapter().parse(page(status=404, text="not found"))
        assert result.status is not StockStatus.IN_STOCK

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
    assert "WARTESCHLANGE OFFEN" in event.title
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

        assert _queue_signals(page(status=403, text=CHALLENGE_HTML)) == []


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
        page(
            status=200, text="<html><body>You are now in line. Estimated wait 12 min</body></html>"
        )
    )
    assert result.status == StockStatus.IN_STOCK
    assert result.alert_title and "OFFEN" in result.alert_title


class TestTheProxyStaysOutOfTheWay:
    """Measured: the server's own IP gets 200, the residential proxy gets 403.

    The adapter passed PROXY_URL unconditionally, so once a proxy was configured
    the queue watch would have been answered 403 (CloudFront) on every check —
    permanently blind to the queue it exists to catch. Unlike the shops, this
    site blocks the proxy and answers us directly.
    """

    @pytest.mark.asyncio
    async def test_no_proxy_is_used_when_the_policy_says_direct(self, monkeypatch):
        import httpx

        from app import proxy as app_proxy
        from app.monitor.adapters.pokemon_center_queue import PokemonCenterQueueAdapter

        monkeypatch.setenv("PROXY_URL", "http://u:p@residential.example:7777")
        monkeypatch.setenv("PROXY_MODE", "on-refusal")
        import app.config as config

        config.get_settings.cache_clear()
        app_proxy.reset()

        used: list[str | None] = []
        real_client = httpx.AsyncClient

        class Recording(real_client):
            def __init__(self, *a, **kw):
                used.append(kw.get("proxy"))
                super().__init__(*a, **kw)

            async def get(self, url, **kw):
                return httpx.Response(200, text="ok", request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx, "AsyncClient", Recording)
        monkeypatch.setattr("app.monitor.adapters.pokemon_center_queue.asyncio.sleep", _noop)

        await PokemonCenterQueueAdapter().fetch("https://www.pokemoncenter.com/de-de")

        assert used == [None], "the queue watch went through the proxy that 403s it"
        config.get_settings.cache_clear()
        app_proxy.reset()


async def _noop(_seconds):
    return None


class TestInterstitialIsNotHealth:
    """A challenge page arrives as HTTP 200 with about a kilobyte of JavaScript.

    Measured on the live server: the direct fetch of /de-de returned 200 with
    1055 characters beginning '<html style="height:100%"><head><META
    NAME="ROBOTS" CONTENT="NOINDEX, NOFOLLOW">', while the real page is 547737
    characters. Judging by status code alone reported that as "store normal, no
    queue" — a watch permanently unable to see a queue, presented as healthy.
    """

    CHALLENGE = CHALLENGE_HTML

    def _page(self, text: str, status: int = 200) -> PageResult:
        return PageResult(
            url="https://www.pokemoncenter.com/de-de",
            final_url="https://www.pokemoncenter.com/de-de",
            status_code=status,
            text=text,
            headers={},
        )

    def test_a_challenge_page_is_unknown_not_idle(self):
        result = PokemonCenterQueueAdapter().parse(self._page(self.CHALLENGE))

        assert result.status is StockStatus.UNKNOWN
        assert "Bot-Prüfseite" in (result.note or "")

    def test_the_note_names_the_field_in_the_dashboard(self):
        """It used to quote JSON for a column with no input field — unusable."""
        note = PokemonCenterQueueAdapter().parse(self._page(self.CHALLENGE)).note or ""
        assert "Abruf-Methode" in note and "Unlocker" in note

    def test_the_real_store_page_is_idle(self):
        real = "<html>" + ("x" * 60000) + '<a href="/de-de/product/box">Box</a></html>'
        result = PokemonCenterQueueAdapter().parse(self._page(real))

        assert result.status is StockStatus.OUT_OF_STOCK
        assert "idle" in (result.note or "")


class TestAQueueWeCannotRead:
    """The two signals that need no guesswork are both gone.

    The redirect and the final URL identify a Queue-it waiting room beyond
    doubt, and the unlocker — the only client that reaches this shop at all —
    destroys both by following the redirect itself. That leaves the body, and
    Pokémon Center customises the waiting room text, so a phrase list is a
    guess. These are the signals that do not depend on the wording.
    """

    def _page(self, text: str, status: int = 200) -> PageResult:
        return PageResult(
            url="https://www.pokemoncenter.com/de-de",
            final_url="https://www.pokemoncenter.com/de-de",
            status_code=status,
            text=text,
            headers={},
        )

    def test_a_queue_it_page_in_an_unknown_language_still_fires(self):
        page = self._page(
            '<html><head><script src="https://static.queue-it.net/script/queueclient.min.js">'
            "</script></head><body><h1>お待ちください</h1></body></html>"
        )
        result = PokemonCenterQueueAdapter().parse(page)

        assert result.status is StockStatus.IN_STOCK
        assert "WARTESCHLANGE OFFEN" in (result.alert_title or "")

    def test_the_queue_it_tag_on_the_normal_store_page_is_not_a_queue(self):
        """It ships with every page, queue or no queue — this is the false
        alarm that would fire on every single check."""
        store = (
            "<html>"
            + ("x" * 60000)
            + '<script src="https://static.queue-it.net/script/queueclient.min.js"></script>'
            + '<a href="/de-de/product/box">Box</a></html>'
        )
        result = PokemonCenterQueueAdapter().parse(self._page(store))

        assert result.status is StockStatus.OUT_OF_STOCK

    def test_an_unrecognised_page_is_an_alarm_not_silence(self):
        """Neither the store nor the challenge: the answer changed shape."""
        result = PokemonCenterQueueAdapter().parse(
            self._page(
                "<html><body><h1>Bitte warten</h1><p>Gleich geht es weiter.</p></body></html>"
            )
        )

        assert result.status is StockStatus.IN_STOCK
        assert "anders aus" in (result.alert_title or "")

    def test_the_known_challenge_is_still_not_an_alarm(self):
        """Otherwise every check on the free path would ping."""
        challenge = TestInterstitialIsNotHealth.CHALLENGE
        result = PokemonCenterQueueAdapter().parse(self._page(challenge))

        assert result.status is StockStatus.UNKNOWN

    def test_a_403_block_page_does_not_ping(self):
        """CloudFront's refusal is small and unfamiliar too — but 403 is how
        this shop answers us at rest, so it is not news."""
        result = PokemonCenterQueueAdapter().parse(
            self._page("<html><body>ERROR: The request could not be satisfied</body></html>", 403)
        )

        assert result.status is StockStatus.UNKNOWN
        assert "kein Urteil" in (result.note or "")

    def test_a_small_page_that_shows_products_is_still_the_store(self):
        small_but_real = '<html><a href="/de-de/product/box">Eine Box</a></html>'
        result = PokemonCenterQueueAdapter().parse(self._page(small_but_real))
        assert result.status is StockStatus.OUT_OF_STOCK

    def test_a_queue_page_still_wins_over_the_size_check(self):
        """A Queue-it holding page is small too — it must never read as blind."""
        queue = "<html><body>Du bist jetzt in der Warteschlange</body></html>"
        result = PokemonCenterQueueAdapter().parse(self._page(queue))

        assert result.status is StockStatus.IN_STOCK
        assert "WARTESCHLANGE" in (result.alert_title or "")
