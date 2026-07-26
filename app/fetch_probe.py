"""Why does curl get 200 where the app gets 429? A/B the request itself.

    docker compose exec -T app python -m app.fetch_probe
    docker compose exec -T app python -m app.fetch_probe https://shop.de/x

Same machine, same IP, same user agent — but a plain curl succeeded while the
app was rate-limited. That leaves the request itself: the proxy it goes through,
the extra headers we send, or the HTTP client's own fingerprint. This fires one
variant at a time, several seconds apart, and prints what came back.

Read the result like this:
  * only "wie die App" fails      -> it is our headers or the proxy, not volume
  * everything fails              -> the shop is throttling this IP right now
  * everything succeeds           -> the block is a reaction to sustained load,
                                     not to any single request
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from app.config import get_settings
from app.monitor.fetchers import BASE_HEADERS

DEFAULT_URL = "https://geeksheaven.de/products.json?limit=250"
GAP_SECONDS = 5.0
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


async def _try(label: str, url: str, *, headers: dict, proxy: str | None) -> None:
    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=30.0, follow_redirects=True, proxy=proxy, http2=False
        ) as client:
            resp = await client.get(url)
        size = len(resp.content)
        note = ""
        if resp.status_code == 429:
            retry = resp.headers.get("Retry-After")
            note = f"  Retry-After={retry}" if retry else ""
            server = resp.headers.get("server")
            note += f"  server={server}" if server else ""
        print(f"{label:34} {resp.status_code}  {size:>8} B{note}")
    except Exception as exc:
        print(f"{label:34} FEHLER  {type(exc).__name__}: {str(exc)[:50]}")


async def probe(url: str) -> None:
    settings = get_settings()
    proxy = settings.proxy_url
    print(f"URL:   {url}")
    print(f"Proxy: {'ja — ' + proxy.split('@')[-1] if proxy else 'nein'}")
    print()

    app_headers = {"User-Agent": settings.user_agent, **BASE_HEADERS}
    # Same headers minus the one that forces a CDN miss on every single request.
    without_no_cache = {k: v for k, v in app_headers.items() if k != "Cache-Control"}

    variants = [
        ("wie die App (mit Proxy)", app_headers, proxy),
        ("wie die App, ohne Proxy", app_headers, None),
        ("ohne Cache-Control", without_no_cache, None),
        ("nur User-Agent (wie curl)", {"User-Agent": CHROME_UA}, None),
    ]
    for index, (label, headers, via) in enumerate(variants):
        if index:
            await asyncio.sleep(GAP_SECONDS)
        await _try(label, url, headers=headers, proxy=via)


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    asyncio.run(probe(url))


if __name__ == "__main__":
    main()
