"""Polite HTTP fetching: shared httpx client, per-domain concurrency caps,
robots.txt cache, retry with backoff, and an optional Playwright fetcher."""

from __future__ import annotations

import asyncio
import logging
import random
import time
import urllib.robotparser
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx

from app import proxy
from app.config import get_settings
from app.monitor import browser_tls
from app.monitor.base import AdapterError, PageResult

log = logging.getLogger(__name__)


class FetchError(AdapterError):
    pass


class RobotsDisallowed(FetchError):
    pass


class RateLimited(FetchError):
    """The shop told us to back off. Nothing is sent until the wait is over."""


_client: httpx.AsyncClient | None = None
_proxy_client: httpx.AsyncClient | None = None
_domain_semaphores: dict[str, asyncio.Semaphore] = {}
_domain_last_request: dict[str, float] = {}  # domain -> monotonic time of last GET
# (domain, bucket, route) -> when we may resume / how often we were refused
_domain_blocked_until: dict[tuple[str, str, str], float] = {}
_domain_penalty: dict[tuple[str, str, str], int] = {}
_robots_cache: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}
_ROBOTS_TTL = 24 * 3600

# A check must finish well inside its own poll interval, otherwise the next run
# collides with it and gets dropped. Never wait longer than this inside one fetch.
MAX_BACKOFF_SECONDS = 8.0
# How long we stand down after a refusal. Doubles with every consecutive one,
# so the rate settles at whatever the shop tolerates.
DEFAULT_COOLDOWN_SECONDS = 60.0
MAX_COOLDOWN_SECONDS = 900.0

# Shopify meters /products.json far more strictly than ordinary pages, and the
# two limits are separate: measured on geeksheaven.de, the catalogue kept
# answering 429 while the scanner's collection pages came back 200 the whole
# time. Tracking one cooldown for the whole shop was wrong in both directions —
# it took the working pages offline, and every successful page reset the
# catalogue's penalty back to zero, so its wait never grew past 60s and
# /products.json never got the quiet stretch it needs.
CATALOG_BUCKET = "catalog"
PAGE_BUCKET = "page"

# Cooldowns belong to the exit address that earned them. A refusal collected on
# our own IP says nothing about a proxy IP, and vice versa — keying them
# together would either bar a route that was never refused, or (worse, once
# every request goes through the proxy) let a refusal be ignored entirely.
DIRECT_ROUTE = "direct"
PROXY_ROUTE = "proxy"


def bucket_of(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    return CATALOG_BUCKET if path.endswith("/products.json") else PAGE_BUCKET


def cooldown_remaining(domain: str, bucket: str = PAGE_BUCKET, route: str = DIRECT_ROUTE) -> float:
    """Seconds until this kind of request may go to this shop over this route."""
    until = _domain_blocked_until.get((domain, bucket, route))
    return 0.0 if until is None else max(0.0, until - time.monotonic())


def note_refusal(
    domain: str,
    retry_after: float | None,
    bucket: str = PAGE_BUCKET,
    route: str = DIRECT_ROUTE,
) -> float:
    """Record a 429/503 and stand down — longer each time it repeats.

    Measured against Cloudflare on a live shop: answering a "Retry-After: 60"
    by retrying after 8 seconds, three times per check, kept the rate limit
    permanently topped up. The ban never expired because we never stopped
    knocking. Waiting the shop out is the only thing that clears it.

    A refused *page* pauses the catalogue too — being turned away on an
    ordinary URL is a sign the whole shop is done with us. A refused
    *catalogue* pauses only itself, because that endpoint has its own budget.
    """
    key = (domain, bucket, route)
    penalty = _domain_penalty.get(key, 0)
    base = retry_after if retry_after and retry_after > 0 else DEFAULT_COOLDOWN_SECONDS
    wait = min(base * (2**penalty), MAX_COOLDOWN_SECONDS)
    _domain_penalty[key] = min(penalty + 1, 8)
    until = time.monotonic() + wait
    _domain_blocked_until[key] = until
    if bucket == PAGE_BUCKET:
        catalog_key = (domain, CATALOG_BUCKET, route)
        _domain_blocked_until[catalog_key] = max(_domain_blocked_until.get(catalog_key, 0.0), until)
    return wait


def note_success(domain: str, bucket: str = PAGE_BUCKET, route: str = DIRECT_ROUTE) -> None:
    """Clear only this bucket — a working page says nothing about the catalogue."""
    _domain_penalty.pop((domain, bucket, route), None)
    _domain_blocked_until.pop((domain, bucket, route), None)


def reset_cooldowns() -> None:
    _domain_blocked_until.clear()
    _domain_penalty.clear()


def cooldown_state() -> dict[str, dict[str, dict[str, float]]]:
    """Per shop, per bucket: what is still blocked and for how long."""
    state: dict[str, dict[str, dict[str, float]]] = {}
    for domain, bucket, route in set(_domain_blocked_until) | set(_domain_penalty):
        label = bucket if route == DIRECT_ROUTE else f"{bucket}@{route}"
        state.setdefault(domain, {})[label] = {
            "remaining_seconds": round(cooldown_remaining(domain, bucket, route), 1),
            "consecutive_refusals": _domain_penalty.get((domain, bucket, route), 0),
        }
    return state


def _refuse_if_cooling(domain: str, bucket: str, route: str) -> None:
    remaining = cooldown_remaining(domain, bucket, route)
    if remaining > 0:
        label = "Katalog" if bucket == CATALOG_BUCKET else "Shop"
        raise RateLimited(f"{domain} drosselt uns ({label}) — Pause noch {remaining:.0f}s")


BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
}


def _domain(url: str) -> str:
    """The throttling key for a URL.

    www.shop.de and shop.de are one shop and share one rate limit, so they must
    share one budget here too — keeping them apart silently doubled our request
    rate on any shop whose watches used both spellings.
    """
    return urlsplit(url).netloc.lower().removeprefix("www.")


@asynccontextmanager
async def domain_gate(
    domain: str, bucket: str = PAGE_BUCKET, *, route: str = DIRECT_ROUTE
) -> AsyncIterator[None]:
    """Serialise requests to one shop and keep a minimum gap between them.

    Every path that touches a shop must go through here. The browser fetcher
    used to skip it, and because one page load pulls in scripts, styles and
    XHRs, a single scan burst past the whole per-minute budget — which is what
    made shops answer 429 to everything else while a plain curl from the same
    machine still got a 200.

    The timestamp is taken when the request *finishes*, so a slow page load or a
    retry chain doesn't get its politeness gap for free.

    A shop under cooldown is refused here, before the semaphore, so 18 waiting
    checks fail instantly instead of queueing up behind a shop that isn't
    answering anyway.
    """
    _refuse_if_cooling(domain, bucket, route)
    async with _semaphore(domain):
        # Check again: 18 watches on one shop all pass the first check together,
        # then queue here. Without this the first one gets the 429 and the other
        # 17 still go out and each collect their own — exactly the knocking that
        # keeps the ban alive.
        _refuse_if_cooling(domain, bucket, route)
        min_gap = get_settings().per_domain_min_interval_seconds
        last = _domain_last_request.get(domain)
        if last is not None:
            wait = min_gap - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
        await asyncio.sleep(random.uniform(0.2, 1.5))  # jitter, don't look like a bot burst
        try:
            yield
        finally:
            _domain_last_request[domain] = time.monotonic()


def get_client(via_proxy: bool = False) -> httpx.AsyncClient:
    """The shared client. `via_proxy` picks the paid route, kept separate so a
    proxied request never reuses a connection opened from our own IP."""
    if via_proxy:
        return _get_proxy_client()
    global _client
    if _client is None or _client.is_closed:
        settings = get_settings()
        _client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent, **BASE_HEADERS},
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
            proxy=None,  # the default route is our own IP and costs nothing
            http2=False,
        )
    return _client


def _get_proxy_client() -> httpx.AsyncClient:
    global _proxy_client
    if _proxy_client is None or _proxy_client.is_closed:
        settings = get_settings()
        _proxy_client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent, **BASE_HEADERS},
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
            proxy=proxy.url(),
            http2=False,
        )
    return _proxy_client


async def close_client() -> None:
    global _client, _proxy_client
    await browser_tls.close()
    for client in (_client, _proxy_client):
        if client is not None and not client.is_closed:
            await client.aclose()
    _client = None
    _proxy_client = None


def _wire_bytes(page: PageResult) -> int:
    """What the proxy actually bills for.

    Bodies travel gzipped, so the decoded text overstates the cost several
    times over — enough to trip a daily budget long before the real volume is
    used. Content-Length is the transferred size when the shop sends it.
    """
    raw = (page.headers or {}).get("content-length") or (page.headers or {}).get("Content-Length")
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass  # duplicated headers arrive as a list, not a number
    return len(page.text.encode())


def _retry_after_of(page: PageResult) -> float | None:
    """Seconds from a Retry-After header, ignoring the HTTP-date form."""
    raw = (page.headers or {}).get("Retry-After") or (page.headers or {}).get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


async def _one_get(url: str, extra_headers: dict[str, str] | None, via_proxy: bool) -> PageResult:
    """One request, over whichever client is in use, as a PageResult.

    Cloudflare fingerprints the TLS handshake, so which client sends the request
    decides whether it is answered at all — measured on this server, httpx got
    429 where curl got 200 in the same second. browser_tls speaks with a real
    Chrome handshake; httpx stays as the fallback when it is not installed.
    """
    settings = get_settings()
    if settings.browser_tls and browser_tls.available():
        return await browser_tls.fetch(
            url,
            extra_headers=extra_headers,
            proxy=proxy.url() if via_proxy else None,
        )
    resp = await get_client(via_proxy).get(url, headers=extra_headers or {})
    json_data = None
    if "json" in resp.headers.get("content-type", ""):
        try:
            json_data = resp.json()
        except ValueError:
            pass
    return PageResult(
        url=url,
        final_url=str(resp.url),
        status_code=resp.status_code,
        text=resp.text,
        json_data=json_data,
        headers=dict(resp.headers),
        fetched_via="httpx",
    )


def _semaphore(domain: str) -> asyncio.Semaphore:
    if domain not in _domain_semaphores:
        _domain_semaphores[domain] = asyncio.Semaphore(get_settings().per_domain_concurrency)
    return _domain_semaphores[domain]


async def _robots_allowed(url: str) -> bool:
    settings = get_settings()
    if not settings.respect_robots_txt:
        return True
    domain = _domain(url)
    cached = _robots_cache.get(domain)
    if cached is None or time.monotonic() - cached[0] > _ROBOTS_TTL:
        parser: urllib.robotparser.RobotFileParser | None = None
        try:
            resp = await get_client().get(f"{urlsplit(url).scheme}://{domain}/robots.txt")
            if resp.status_code == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(resp.text.splitlines())
        except httpx.HTTPError:
            parser = None  # unreachable robots.txt -> assume allowed
        _robots_cache[domain] = (time.monotonic(), parser)
        cached = _robots_cache[domain]
    parser = cached[1]
    if parser is None:
        return True
    return parser.can_fetch(get_settings().user_agent, url)


async def fetch_httpx(
    url: str,
    *,
    extra_headers: dict[str, str] | None = None,
    respect_robots: bool = True,
    retries: int = 3,
) -> PageResult:
    """GET with jitter, per-domain concurrency cap, and exponential backoff."""
    if respect_robots and not await _robots_allowed(url):
        raise RobotsDisallowed(f"robots.txt disallows fetching {url}")
    domain = _domain(url)
    bucket = bucket_of(url)
    via_proxy = proxy.routes_via_proxy(domain, bucket)
    route = PROXY_ROUTE if via_proxy else DIRECT_ROUTE
    last_exc: Exception | None = None
    async with domain_gate(domain, bucket, route=route):
        for attempt in range(retries):
            start = time.monotonic()
            try:
                page = await _one_get(url, extra_headers, via_proxy)
            except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
                last_exc = exc
                log.warning("fetch attempt %d failed for %s: %s", attempt + 1, url, exc)
                await asyncio.sleep(min(2**attempt, MAX_BACKOFF_SECONDS) + random.uniform(0, 1))
                continue
            page.elapsed_ms = int((time.monotonic() - start) * 1000)
            if page.status_code in (429, 503):
                # Do not retry. Retrying a refusal inside the same check is what
                # kept the Cloudflare rate limit permanently topped up: the shop
                # asked for 60 s and got knocked on again after 8, three times
                # per check, from 18 watches. Stand down for the whole domain
                # and let the scheduler come back once the shop has cooled off.
                wait = note_refusal(domain, _retry_after_of(page), bucket, route)
                escalated = proxy.note_refusal(domain, bucket, was_proxied=via_proxy)
                log.warning(
                    "HTTP %d from %s (%s) — Pause %.0fs%s",
                    page.status_code,
                    domain,
                    bucket,
                    wait,
                    ", ab jetzt über den Proxy" if escalated else "",
                )
                raise RateLimited(f"{domain} antwortet {page.status_code} — Pause {wait:.0f}s")
            note_success(domain, bucket, route)
            proxy.note_success(domain, bucket, _wire_bytes(page), was_proxied=via_proxy)
            return page
    raise FetchError(f"all fetch attempts failed for {url}: {last_exc}")


# ---------------------------------------------------------------------------
# Scraping API — for JS-rendered + bot-protected shops (MediaMarkt, Smyths, …)
# where our own httpx/Playwright + residential proxy still get blocked. The
# provider runs the render + anti-bot bypass on their side and returns HTML.
# ---------------------------------------------------------------------------


def _scraper_request(target: str) -> tuple[str, dict]:
    """Build (endpoint, query params) for the configured scraping provider."""
    s = get_settings()
    tier = s.scraper_api_tier
    if s.scraper_api_provider == "scrapingbee":
        params = {
            "api_key": s.scraper_api_key,
            "url": target,
            "render_js": "true" if s.scraper_api_render_js else "false",
            "country_code": s.scraper_api_country,
        }
        if tier in ("premium", "ultra_premium"):
            params["premium_proxy"] = "true"
        if tier == "ultra_premium":
            params["stealth_proxy"] = "true"
        return "https://app.scrapingbee.com/api/v1/", params
    # default: scraperapi
    params = {"api_key": s.scraper_api_key, "url": target, "country_code": s.scraper_api_country}
    if s.scraper_api_render_js:
        params["render"] = "true"
    if tier == "premium":
        params["premium"] = "true"
    elif tier == "ultra_premium":
        params["ultra_premium"] = "true"
    return "http://api.scraperapi.com/", params


async def _fetch_brightdata(
    url: str, settings, start: float, extra_headers: dict[str, str] | None
) -> PageResult:
    """Bright Data Web Unlocker API (POST, Bearer auth, raw HTML back)."""
    if not settings.brightdata_zone:
        raise FetchError("brightdata provider needs BRIGHTDATA_ZONE set")
    payload = {
        "zone": settings.brightdata_zone,
        "url": url,
        "format": "raw",
        "country": settings.scraper_api_country,
    }
    headers = {
        "Authorization": f"Bearer {settings.scraper_api_key}",
        "Content-Type": "application/json",
        **(extra_headers or {}),
    }
    async with httpx.AsyncClient(timeout=settings.scraper_api_timeout_seconds) as client:
        try:
            resp = await client.post(
                "https://api.brightdata.com/request", json=payload, headers=headers
            )
        except httpx.HTTPError as exc:
            raise FetchError(
                f"brightdata fetch failed for {url}: {type(exc).__name__}: {exc}"
            ) from exc
    if resp.status_code in (401, 403):
        raise FetchError(
            f"brightdata auth error (HTTP {resp.status_code}) — check API token / BRIGHTDATA_ZONE"
        )
    if not resp.text.strip():
        # The 200 above is the *API's* status, not the target's. An empty body
        # means the unlocker never got the page — reporting that as a
        # successful fetch turns it into a silent "0 products found" further
        # up, which sends the debugging in completely the wrong direction.
        detail = ", ".join(
            f"{k}={v}" for k, v in resp.headers.items() if k.lower().startswith(("x-", "brd"))
        )
        raise FetchError(
            f"brightdata returned an empty body for {url}"
            + (f" ({detail})" if detail else " (no diagnostic headers)")
        )
    return PageResult(
        url=url,
        final_url=url,
        status_code=resp.status_code,
        text=resp.text,
        headers=dict(resp.headers),
        elapsed_ms=int((time.monotonic() - start) * 1000),
        fetched_via="scraperapi:brightdata",
    )


async def fetch_scraperapi(url: str, *, extra_headers: dict[str, str] | None = None) -> PageResult:
    """Fetch a URL through the configured scraping API. Falls back to plain
    httpx when no SCRAPER_API_KEY is set, so watches never hard-fail."""
    settings = get_settings()
    if not settings.scraper_api_key:
        log.warning("scraperapi requested but SCRAPER_API_KEY unset — falling back to httpx")
        return await fetch_httpx(url, extra_headers=extra_headers, respect_robots=False)
    start = time.monotonic()
    if settings.scraper_api_provider == "brightdata":
        return await _fetch_brightdata(url, settings, start, extra_headers)
    endpoint, params = _scraper_request(url)
    # The provider supplies its own proxies — call it directly (no proxy_url).
    async with httpx.AsyncClient(
        timeout=settings.scraper_api_timeout_seconds, follow_redirects=True
    ) as client:
        try:
            resp = await client.get(endpoint, params=params, headers=extra_headers or {})
        except httpx.HTTPError as exc:
            raise FetchError(f"scraperapi fetch failed for {url}: {exc}") from exc
    if resp.status_code in (401, 403):
        raise FetchError(
            f"scraperapi auth/quota error (HTTP {resp.status_code}) — check SCRAPER_API_KEY / credits"
        )
    return PageResult(
        url=url,
        final_url=str(resp.url),
        status_code=resp.status_code,
        text=resp.text,
        headers=dict(resp.headers),
        elapsed_ms=int((time.monotonic() - start) * 1000),
        fetched_via=f"scraperapi:{settings.scraper_api_provider}",
    )


# ---------------------------------------------------------------------------
# Playwright (optional) — for JS-heavy / anti-bot sites
# ---------------------------------------------------------------------------

_pw = None
_browser = None
_pw_lock = asyncio.Lock()


async def _get_browser():
    global _pw, _browser
    async with _pw_lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise FetchError(
                "playwright is not installed — `pip install 'tcg-tracker[playwright]'` "
                "and `playwright install chromium`"
            ) from exc
        _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(headless=True)
        return _browser


def _playwright_proxy(proxy_url: str | None) -> dict | None:
    """Playwright needs server + separate username/password, not embedded creds."""
    if not proxy_url:
        return None
    split = urlsplit(proxy_url)
    server = f"{split.scheme}://{split.hostname}"
    if split.port:
        server += f":{split.port}"
    proxy: dict = {"server": server}
    if split.username:
        proxy["username"] = split.username
    if split.password:
        proxy["password"] = split.password
    return proxy


async def fetch_playwright(
    url: str,
    *,
    extra_headers: dict[str, str] | None = None,
    wait_ms: int = 2500,
    idle_timeout_ms: int = 15000,
) -> PageResult:
    domain = _domain(url)
    settings = get_settings()
    async with domain_gate(domain):
        browser = await _get_browser()
        context = await browser.new_context(
            user_agent=settings.user_agent,
            locale="de-DE",
            extra_http_headers=extra_headers or {},
            proxy=_playwright_proxy(proxy.url()),
        )
        start = time.monotonic()
        try:
            page = await context.new_page()
            # Keep the burst small: every sub-request counts against the shop's
            # rate limit, and we only ever read text. Scripts stay — they are
            # what renders the product grid we came for.
            await page.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in ("image", "media", "font", "stylesheet")
                    else route.continue_()
                ),
            )
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # A fixed sleep is a guess: on a slow run the product grid hasn't
            # been fetched yet and we'd silently snapshot a half-empty page
            # (scanner then reports "0 products"). Wait for the XHRs to settle
            # first; pages that never go idle (ads, polling) still fall through
            # to the fixed delay below.
            try:
                await page.wait_for_load_state("networkidle", timeout=idle_timeout_ms)
            except Exception:  # noqa: S110 - best effort, the fixed wait still applies
                pass
            await page.wait_for_timeout(wait_ms)
            html = await page.content()
            status = resp.status if resp else 0
            final_url = page.url
        except Exception as exc:
            raise FetchError(f"playwright fetch failed for {url}: {exc}") from exc
        finally:
            await context.close()
        return PageResult(
            url=url,
            final_url=final_url,
            status_code=status,
            text=html,
            elapsed_ms=int((time.monotonic() - start) * 1000),
            fetched_via="playwright",
        )


async def shutdown_playwright() -> None:
    global _pw, _browser
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:  # noqa: S110 - best-effort shutdown
            pass
        _browser = None
    if _pw is not None:
        try:
            await _pw.stop()
        except Exception:
            pass
        _pw = None
