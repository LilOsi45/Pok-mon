"""An HTTP client that Cloudflare does not recognise as a script.

Measured on the live server, same URL, same IP, within seconds of each other:

    httpx, app headers          429   18 B   server=cloudflare
    httpx, curl's headers       429   18 B   server=cloudflare
    curl                        200   936 KB

Four httpx variants differing in headers and proxy all failed; the one request
that differed in *program* succeeded. Cloudflare fingerprints the TLS handshake
and the HTTP/2 settings, and Python's default stack has a distinctive one. No
header, no delay and no proxy changes that — a proxy would have carried the same
fingerprint to a new address and bought the same 429.

curl_cffi speaks through libcurl-impersonate, which reproduces a real Chrome
handshake, so the request looks like the browser it claims to be in the User-
Agent. Everything else — the politeness gate, cooldowns, the shared catalogue —
is unchanged; only the wire-level identity differs.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.monitor.base import PageResult

log = logging.getLogger(__name__)

# libcurl-impersonate target. "chrome" tracks the newest Chrome profile the
# installed version ships, so it stays current without a code change.
IMPERSONATE = "chrome"

# Keyed by proxy URL: one session per exit route. Sharing a single session would
# silently keep sending through whichever route happened to be used first.
_sessions: dict[str | None, Any] = {}
_available: bool | None = None


def available() -> bool:
    """Whether curl_cffi is installed. Missing means we fall back to httpx."""
    global _available
    if _available is None:
        try:
            import curl_cffi.requests  # noqa: F401

            _available = True
        except Exception as exc:  # pragma: no cover - depends on the image
            log.warning("curl_cffi unavailable, staying on httpx: %s", exc)
            _available = False
    return _available


def _get_session(proxy: str | None) -> Any:
    from curl_cffi import requests

    if proxy not in _sessions:
        settings = get_settings()
        _sessions[proxy] = requests.AsyncSession(
            impersonate=IMPERSONATE,
            timeout=settings.request_timeout_seconds,
            # Let the impersonated profile supply Accept/Accept-Language and the
            # header order; overriding them is exactly what gives a script away.
            headers={},
            proxies={"http": proxy, "https": proxy} if proxy else None,
        )
    return _sessions[proxy]


async def close() -> None:
    for session in list(_sessions.values()):
        try:
            await session.close()
        except Exception:  # noqa: S110 - best-effort shutdown
            pass
    _sessions.clear()


def reset_for_tests() -> None:
    global _available
    _sessions.clear()
    _available = None


async def fetch(
    url: str,
    *,
    extra_headers: dict[str, str] | None = None,
    proxy: str | None = None,
) -> PageResult:
    """One GET with a browser handshake. Raises on transport errors."""
    session = _get_session(proxy)
    resp = await session.get(url, headers=extra_headers or None, allow_redirects=True)

    json_data = None
    if "json" in (resp.headers.get("content-type") or ""):
        try:
            json_data = resp.json()
        except Exception:  # noqa: S110 - a bad body is not fatal here
            json_data = None

    return PageResult(
        url=url,
        final_url=str(resp.url),
        status_code=resp.status_code,
        text=resp.text,
        json_data=json_data,
        headers=dict(resp.headers),
        elapsed_ms=int(getattr(resp, "elapsed", 0) * 1000),
        fetched_via="browser-tls",
    )
