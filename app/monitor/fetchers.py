"""Polite HTTP fetching: shared httpx client, per-domain concurrency caps,
robots.txt cache, retry with backoff, and an optional Playwright fetcher."""

from __future__ import annotations

import asyncio
import logging
import random
import time
import urllib.robotparser
from urllib.parse import urlsplit

import httpx

from app.config import get_settings
from app.monitor.base import AdapterError, PageResult

log = logging.getLogger(__name__)


class FetchError(AdapterError):
    pass


class RobotsDisallowed(FetchError):
    pass


_client: httpx.AsyncClient | None = None
_domain_semaphores: dict[str, asyncio.Semaphore] = {}
_domain_last_request: dict[str, float] = {}  # domain -> monotonic time of last GET
_robots_cache: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}
_ROBOTS_TTL = 24 * 3600

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
}


def _domain(url: str) -> str:
    return urlsplit(url).netloc.lower()


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        settings = get_settings()
        _client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent, **BASE_HEADERS},
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
            proxy=settings.proxy_url,
            http2=False,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


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
    last_exc: Exception | None = None
    async with _semaphore(domain):
        # Per-domain throttle: keep a minimum gap between requests to the same
        # shop so many watches on one domain don't burst into a 429.
        min_gap = get_settings().per_domain_min_interval_seconds
        last = _domain_last_request.get(domain)
        if last is not None:
            wait = min_gap - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
        await asyncio.sleep(random.uniform(0.2, 1.5))  # jitter, don't look like a bot burst
        _domain_last_request[domain] = time.monotonic()
        for attempt in range(retries):
            start = time.monotonic()
            try:
                resp = await get_client().get(url, headers=extra_headers or {})
            except httpx.HTTPError as exc:
                last_exc = exc
                log.warning("fetch attempt %d failed for %s: %s", attempt + 1, url, exc)
                await asyncio.sleep((2**attempt) + random.uniform(0, 1))
                continue
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if resp.status_code in (429, 503) and attempt < retries - 1:
                wait = float(resp.headers.get("Retry-After", 2**attempt * 2))
                log.warning("HTTP %d from %s, backing off %.1fs", resp.status_code, domain, wait)
                await asyncio.sleep(min(wait, 60) + random.uniform(0, 1))
                continue
            json_data = None
            content_type = resp.headers.get("content-type", "")
            if "json" in content_type:
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
                elapsed_ms=elapsed_ms,
                fetched_via="httpx",
            )
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


async def fetch_scraperapi(
    url: str, *, extra_headers: dict[str, str] | None = None
) -> PageResult:
    """Fetch a URL through the configured scraping API. Falls back to plain
    httpx when no SCRAPER_API_KEY is set, so watches never hard-fail."""
    settings = get_settings()
    if not settings.scraper_api_key:
        log.warning("scraperapi requested but SCRAPER_API_KEY unset — falling back to httpx")
        return await fetch_httpx(url, extra_headers=extra_headers, respect_robots=False)
    endpoint, params = _scraper_request(url)
    start = time.monotonic()
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
    url: str, *, extra_headers: dict[str, str] | None = None, wait_ms: int = 2500
) -> PageResult:
    domain = _domain(url)
    settings = get_settings()
    async with _semaphore(domain):
        await asyncio.sleep(random.uniform(0.2, 1.5))
        browser = await _get_browser()
        context = await browser.new_context(
            user_agent=settings.user_agent,
            locale="de-DE",
            extra_http_headers=extra_headers or {},
            proxy=_playwright_proxy(settings.proxy_url),
        )
        start = time.monotonic()
        try:
            page = await context.new_page()
            # keep it light: skip images/fonts/media
            await page.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in ("image", "media", "font")
                    else route.continue_()
                ),
            )
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
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
