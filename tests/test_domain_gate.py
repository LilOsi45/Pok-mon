"""Every path to a shop must pass the same per-domain throttle.

Measured on the live server: a plain `curl` to geeksheaven.de/products.json got
200 while the app got 429 on the same machine, same IP, same user agent. The app
was simply asking far more often than its configured budget, because two paths
leaked around the throttle:

  * the browser fetcher took the concurrency semaphore but neither waited for
    the minimum gap nor recorded that it had just hit the shop — and one page
    load pulls in scripts, styles and XHRs, so a single scan burst past the
    whole per-minute budget;
  * www.shop.de and shop.de were tracked as two different shops, so a mix of
    both spellings doubled the allowed rate against one real rate limit.
"""

from __future__ import annotations

import asyncio

import pytest

from app.monitor import fetchers

# Patching fetchers.asyncio.sleep patches the real asyncio module, so the
# replacement must call the original it captured, not asyncio.sleep again.
_REAL_SLEEP = asyncio.sleep


async def _instant(_seconds: float) -> None:
    await _REAL_SLEEP(0)


@pytest.fixture(autouse=True)
def _clean_throttle_state(monkeypatch):
    monkeypatch.setenv("PER_DOMAIN_MIN_INTERVAL_SECONDS", "20")
    monkeypatch.setenv("PER_DOMAIN_CONCURRENCY", "1")
    import app.config as config

    config.get_settings.cache_clear()
    fetchers.reset_cooldowns()
    fetchers._domain_last_request.clear()
    fetchers._domain_semaphores.clear()
    yield
    fetchers.reset_cooldowns()
    fetchers._domain_last_request.clear()
    fetchers._domain_semaphores.clear()
    config.get_settings.cache_clear()


class TestDomainKey:
    def test_www_and_bare_host_share_one_budget(self):
        assert fetchers._domain("https://www.geeksheaven.de/products/a") == fetchers._domain(
            "https://geeksheaven.de/products/b"
        )

    def test_different_shops_stay_apart(self):
        assert fetchers._domain("https://a.de/x") != fetchers._domain("https://b.de/x")

    def test_subdomains_are_not_collapsed(self):
        """Only the www prefix is cosmetic — shop.example.de is its own host."""
        assert fetchers._domain("https://shop.example.de/x") != fetchers._domain(
            "https://example.de/x"
        )


@pytest.mark.asyncio
class TestGate:
    async def test_the_gap_is_enforced_between_two_requests(self, monkeypatch):
        waits: list[float] = []

        async def record(seconds: float) -> None:
            waits.append(seconds)

        monkeypatch.setattr(fetchers.asyncio, "sleep", record)

        async with fetchers.domain_gate("shop.example"):
            pass
        async with fetchers.domain_gate("shop.example"):
            pass

        # first pass: jitter only; second: jitter plus the remaining gap
        assert len(waits) == 3
        assert max(waits) > 15, f"second request was not held back: {waits}"

    async def test_the_timestamp_is_taken_after_the_work(self, monkeypatch):
        """A slow page load must not earn its politeness gap for free."""
        monkeypatch.setattr(fetchers.asyncio, "sleep", _instant)

        async with fetchers.domain_gate("shop.example"):
            before = fetchers._domain_last_request.get("shop.example")
        after = fetchers._domain_last_request.get("shop.example")

        assert before is None, "recorded before the request even ran"
        assert after is not None

    async def test_a_failing_request_still_counts(self, monkeypatch):
        """It reached the shop, so it must count against the budget."""
        monkeypatch.setattr(fetchers.asyncio, "sleep", _instant)

        with pytest.raises(RuntimeError):
            async with fetchers.domain_gate("shop.example"):
                raise RuntimeError("boom")

        assert "shop.example" in fetchers._domain_last_request

    async def test_requests_to_one_shop_do_not_overlap(self, monkeypatch):
        monkeypatch.setattr(fetchers.asyncio, "sleep", _instant)
        running = 0
        peak = 0

        async def one() -> None:
            nonlocal running, peak
            async with fetchers.domain_gate("shop.example"):
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0)
                running -= 1

        await asyncio.gather(*(one() for _ in range(5)))
        assert peak == 1


@pytest.mark.asyncio
class TestBothFetchersUseTheGate:
    async def test_httpx_goes_through_the_gate(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(fetchers, "domain_gate", _spy_gate(seen))
        monkeypatch.setattr(fetchers, "_robots_allowed", _true)
        monkeypatch.setattr(fetchers, "get_client", lambda: _OkClient())

        await fetchers.fetch_httpx("https://www.shop.example/p")

        assert seen == ["shop.example"]

    async def test_playwright_goes_through_the_gate(self, monkeypatch):
        """The regression: the browser path used to bypass the gap entirely."""
        seen: list[str] = []
        monkeypatch.setattr(fetchers, "domain_gate", _spy_gate(seen))

        async def boom():
            raise RuntimeError("no browser in tests")

        monkeypatch.setattr(fetchers, "_get_browser", boom)

        # The gate is entered before the browser is touched, so the fetch
        # failing here is expected — what matters is that it was entered.
        with pytest.raises(RuntimeError, match="no browser"):
            await fetchers.fetch_playwright("https://www.shop.example/c")

        assert seen == ["shop.example"], "browser fetch skipped the per-domain throttle"


# --- helpers ---------------------------------------------------------------


def _spy_gate(seen: list[str]):
    from contextlib import asynccontextmanager

    from app.monitor.fetchers import PAGE_BUCKET

    @asynccontextmanager
    async def gate(domain: str, bucket: str = PAGE_BUCKET):
        seen.append(domain)
        yield

    return gate


async def _true(_url: str) -> bool:
    return True


class _OkClient:
    async def get(self, url, **_kw):
        import httpx

        return httpx.Response(200, text="ok", request=httpx.Request("GET", url))
