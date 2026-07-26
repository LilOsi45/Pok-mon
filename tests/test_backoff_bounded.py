"""A single check must never block longer than its own poll interval.

Honouring a shop's 60 s Retry-After inside one fetch made a check take minutes.
The next scheduled run then collided with the still-running one and APScheduler
dropped it — "maximum number of running instances reached (1)" — so every watch
on a rate-limiting shop reported as stale while nothing was actually broken.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from app.monitor import fetchers

URL = "https://shop.example/p"


@pytest.fixture(autouse=True)
def _no_domain_gap():
    """fetch_httpx also sleeps to keep a gap between requests to one domain.

    That gap is tracked in a module-level dict, so a second test on the same
    host would inherit a ~20 s sleep from the first and be mistaken for a
    backoff. Clear it so the only sleeps left are the polite jitter and the
    backoff under test.
    """
    fetchers._domain_last_request.clear()
    yield
    fetchers._domain_last_request.clear()


class Recorder:
    """Captures how long the fetcher wanted to sleep."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def _resp(status: int, *, text: str = "", headers: dict | None = None) -> httpx.Response:
    """httpx needs the originating request attached before .url/.request work."""
    return httpx.Response(
        status, text=text, headers=headers or {}, request=httpx.Request("GET", URL)
    )


def _client_returning(*responses: httpx.Response):
    calls = iter(responses)

    class FakeClient:
        async def get(self, *_args, **_kwargs):
            return next(calls)

    return FakeClient()


def _patch_fetcher(monkeypatch, sleeper: Recorder, *responses: httpx.Response) -> None:
    # One client for the whole fetch: get_client() is called per attempt, so
    # building it inside the lambda would restart the response iterator and the
    # retry would see the first response again.
    client = _client_returning(*responses)
    monkeypatch.setattr(fetchers.asyncio, "sleep", sleeper)
    monkeypatch.setattr(fetchers, "get_client", lambda: client)
    monkeypatch.setattr(fetchers, "_robots_allowed", AsyncMock(return_value=True))


# The first sleep of every fetch is the anti-burst jitter, random.uniform(0.2, 1.5).
JITTER_MAX = 1.5


@pytest.mark.asyncio
async def test_retry_after_is_capped(monkeypatch):
    """A 60 s Retry-After must not become a 60 s stall."""
    sleeper = Recorder()
    _patch_fetcher(
        monkeypatch,
        sleeper,
        _resp(429, headers={"Retry-After": "60"}),
        _resp(200, text="<html>ok</html>"),
    )

    page = await fetchers.fetch_httpx(URL)

    assert page.status_code == 200
    jitter, backoff = sleeper.waits  # exactly one jitter + one backoff
    assert jitter <= JITTER_MAX
    assert backoff <= fetchers.MAX_BACKOFF_SECONDS + 1, f"waited {backoff:.0f}s on a 60s header"


@pytest.mark.asyncio
async def test_total_stall_stays_under_a_poll_interval(monkeypatch):
    """Worst case — every attempt rate-limited — must still be short."""
    sleeper = Recorder()
    _patch_fetcher(
        monkeypatch, sleeper, *(_resp(429, headers={"Retry-After": "600"}) for _ in range(3))
    )

    page = await fetchers.fetch_httpx(URL)

    assert page.status_code == 429  # reported, not retried forever
    assert sum(sleeper.waits) < 60, f"stalled {sum(sleeper.waits):.0f}s inside one check"


@pytest.mark.asyncio
async def test_successful_fetch_does_not_back_off(monkeypatch):
    sleeper = Recorder()
    _patch_fetcher(monkeypatch, sleeper, _resp(200, text="ok"))

    page = await fetchers.fetch_httpx(URL)

    assert page.status_code == 200
    assert len(sleeper.waits) == 1  # the jitter, nothing else
    assert sleeper.waits[0] <= JITTER_MAX


@pytest.mark.asyncio
async def test_the_domain_gap_is_not_mistaken_for_a_backoff(monkeypatch):
    """Guards the fixture above: the gap sleep is real and much longer."""
    monkeypatch.setattr(fetchers, "_domain_last_request", {fetchers._domain(URL): 0.0})
    sleeper = Recorder()
    _patch_fetcher(monkeypatch, sleeper, _resp(200, text="ok"))

    await fetchers.fetch_httpx(URL)

    # time.monotonic() is far past 0, so the computed gap is negative -> skipped.
    assert len(sleeper.waits) == 1


def test_module_constant_is_documented():
    assert isinstance(fetchers.MAX_BACKOFF_SECONDS, float)
    # Must stay well under the shortest poll interval (120 s) even x3 attempts.
    assert 0 < fetchers.MAX_BACKOFF_SECONDS <= 15
