"""When a shop refuses us, stop knocking — that is what clears the ban.

Measured on the live server against Cloudflare: every variant of the request
(with proxy, without proxy, without Cache-Control, and a bare curl-equivalent)
came back 429 with "Retry-After: 60". The IP was genuinely rate-limited, and it
stayed that way because the fetcher answered a 60-second refusal by retrying
after 8 seconds, three times per check, from 18 watches on that one shop. The
limit was being topped up faster than it could drain.

The contract now: a 429 or 503 puts the whole domain on ice for as long as the
shop asked, doubling if it keeps refusing, and every check in that window fails
instantly without touching the network.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app import proxy
from app.monitor import fetchers

URL = "https://shop.example/p"
DOMAIN = "shop.example"

_REAL_SLEEP = asyncio.sleep


async def _instant(_seconds: float) -> None:
    await _REAL_SLEEP(0)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    proxy.reset()
    fetchers.reset_cooldowns()
    fetchers._domain_last_request.clear()
    fetchers._domain_semaphores.clear()
    monkeypatch.setattr(fetchers.asyncio, "sleep", _instant)
    monkeypatch.setattr(fetchers, "_robots_allowed", AsyncMock(return_value=True))
    yield
    fetchers.reset_cooldowns()
    fetchers._domain_last_request.clear()
    fetchers._domain_semaphores.clear()


def _resp(status: int, *, text: str = "", headers: dict | None = None) -> httpx.Response:
    """httpx needs the originating request attached before .url/.request work."""
    return httpx.Response(
        status, text=text, headers=headers or {}, request=httpx.Request("GET", URL)
    )


def _serving(*responses: httpx.Response):
    """One client for the whole fetch — get_client() is called per attempt."""
    calls = iter(responses)

    class FakeClient:
        count = 0

        async def get(self, *_args, **_kwargs):
            FakeClient.count += 1
            return next(calls)

    return FakeClient()


class TestCooldownBookkeeping:
    def test_a_refusal_honours_retry_after(self):
        assert fetchers.note_refusal(DOMAIN, 60.0) == 60.0
        assert 59 <= fetchers.cooldown_remaining(DOMAIN) <= 60

    def test_missing_header_falls_back_to_a_sane_default(self):
        assert fetchers.note_refusal(DOMAIN, None) == fetchers.DEFAULT_COOLDOWN_SECONDS

    def test_repeated_refusals_back_off_further(self):
        """A shop that keeps saying no gets asked less and less often."""
        waits = [fetchers.note_refusal(DOMAIN, 60.0) for _ in range(4)]
        assert waits == [60.0, 120.0, 240.0, 480.0]

    def test_the_wait_is_capped(self):
        for _ in range(20):
            wait = fetchers.note_refusal(DOMAIN, 600.0)
        assert wait == fetchers.MAX_COOLDOWN_SECONDS

    def test_success_clears_the_penalty(self):
        fetchers.note_refusal(DOMAIN, 60.0)
        fetchers.note_refusal(DOMAIN, 60.0)
        fetchers.note_success(DOMAIN)

        assert fetchers.cooldown_remaining(DOMAIN) == 0
        assert fetchers.note_refusal(DOMAIN, 60.0) == 60.0, "penalty was not reset"

    def test_shops_cool_down_independently(self):
        fetchers.note_refusal(DOMAIN, 60.0)
        assert fetchers.cooldown_remaining("other.example") == 0


@pytest.mark.asyncio
class TestFetchBehaviour:
    async def test_a_429_is_not_retried(self, monkeypatch):
        """The regression: three knocks per check kept the ban alive."""
        client = _serving(*(_resp(429, headers={"Retry-After": "60"}) for _ in range(3)))
        type(client).count = 0
        monkeypatch.setattr(fetchers, "get_client", lambda *_a, **_k: client)

        with pytest.raises(fetchers.RateLimited):
            await fetchers.fetch_httpx(URL)

        assert type(client).count == 1, "the shop was knocked on again after refusing"

    async def test_the_whole_shop_goes_quiet_after_one_refusal(self, monkeypatch):
        """18 watches on one shop must not each send their own request."""
        client = _serving(_resp(429, headers={"Retry-After": "60"}))
        type(client).count = 0
        monkeypatch.setattr(fetchers, "get_client", lambda *_a, **_k: client)

        results = await asyncio.gather(
            *(fetchers.fetch_httpx(f"https://shop.example/p{i}") for i in range(18)),
            return_exceptions=True,
        )

        assert all(isinstance(r, fetchers.RateLimited) for r in results)
        assert type(client).count == 1, f"{type(client).count} requests to a shop that said no"

    async def test_a_blocked_shop_costs_no_time(self, monkeypatch):
        """Failing fast is the point — a stalled check gets dropped by the scheduler."""
        fetchers.note_refusal(DOMAIN, 600.0)
        monkeypatch.setattr(fetchers, "get_client", lambda *_a, **_k: _serving(_resp(200)))

        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(fetchers.RateLimited):
            await fetchers.fetch_httpx(URL)
        assert loop.time() - start < 0.5

    async def test_503_is_treated_the_same(self, monkeypatch):
        monkeypatch.setattr(fetchers, "get_client", lambda *_a, **_k: _serving(_resp(503)))

        with pytest.raises(fetchers.RateLimited):
            await fetchers.fetch_httpx(URL)
        assert fetchers.cooldown_remaining(DOMAIN) > 0

    async def test_a_good_answer_clears_an_earlier_penalty(self, monkeypatch):
        fetchers.note_refusal(DOMAIN, 60.0)
        fetchers.reset_cooldowns()  # the wait has passed
        fetchers._domain_penalty[(DOMAIN, fetchers.PAGE_BUCKET)] = 3  # still on probation
        monkeypatch.setattr(fetchers, "get_client", lambda *_a, **_k: _serving(_resp(200, text="ok")))

        page = await fetchers.fetch_httpx(URL)

        assert page.status_code == 200
        assert fetchers._domain_penalty.get((DOMAIN, fetchers.PAGE_BUCKET)) is None

    async def test_other_errors_still_bubble_up_normally(self, monkeypatch):
        """A 404 is the shop answering, not refusing — no cooldown."""
        monkeypatch.setattr(fetchers, "get_client", lambda *_a, **_k: _serving(_resp(404)))

        page = await fetchers.fetch_httpx(URL)

        assert page.status_code == 404
        assert fetchers.cooldown_remaining(DOMAIN) == 0

    async def test_an_http_date_retry_after_does_not_crash(self, monkeypatch):
        headers = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
        monkeypatch.setattr(fetchers, "get_client", lambda *_a, **_k: _serving(_resp(429, headers=headers)))

        with pytest.raises(fetchers.RateLimited):
            await fetchers.fetch_httpx(URL)
        assert fetchers.cooldown_remaining(DOMAIN) == pytest.approx(
            fetchers.DEFAULT_COOLDOWN_SECONDS, abs=1
        )
