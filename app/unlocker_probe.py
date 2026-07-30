"""What does the unlocker actually return for one URL?

    docker compose exec -T app python -m app.unlocker_probe "https://www.pokemoncenter.com/de-de/category/new-releases?category=tcg-cards"

A Pokémon Center drop was missed while the scanner reported "brightdata
returned an empty body". Two things have to be true for a scan to work and the
error only proves the first one failed:

  1. the unlocker has to hand back the page at all;
  2. that page has to contain the products — Pokémon Center is a JavaScript
     app, so plain HTML can arrive complete *and* empty of products.

So this tries the request in several shapes, and for each one reports the size,
Bright Data's own diagnostic headers, and how many products the scanner's own
parser finds in the result. The last number is the one that decides whether a
drop would be seen.
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from app.config import get_settings

API = "https://api.brightdata.com/request"
DEFAULT_URL = "https://www.pokemoncenter.com/de-de/category/new-releases?category=tcg-cards"


def _diagnostics(resp: httpx.Response) -> str:
    interesting = {
        k: v
        for k, v in resp.headers.items()
        if k.lower().startswith(("x-brd", "x-response", "brd"))
    }
    return ", ".join(f"{k}={v}" for k, v in interesting.items()) or "keine"


async def _attempt(label: str, url: str, extra_payload: dict) -> None:
    settings = get_settings()
    payload = {
        "zone": settings.brightdata_zone,
        "url": url,
        "format": "raw",
        "country": settings.scraper_api_country,
        **extra_payload,
    }
    headers = {
        "Authorization": f"Bearer {settings.scraper_api_key}",
        "Content-Type": "application/json",
    }
    print(f"--- {label}")
    try:
        async with httpx.AsyncClient(timeout=settings.scraper_api_timeout_seconds) as client:
            resp = await client.post(API, json=payload, headers=headers)
    except Exception as exc:
        print(f"    FEHLER {type(exc).__name__}: {str(exc)[:120]}\n")
        return

    body = resp.text
    print(f"    API-Status    : {resp.status_code}")
    print(f"    Größe         : {len(body)} Zeichen")
    print(f"    Brightdata    : {_diagnostics(resp)}")
    if not body.strip():
        print("    => LEER, die Seite kam nie an.\n")
        return

    from app.scanner import parse_listing

    try:
        found = parse_listing(body, url)
    except Exception as exc:
        found = []
        print(f"    Parser-Fehler : {type(exc).__name__}: {exc}")
    print(f"    Produkte      : {len(found)}   <- entscheidet, ob ein Drop auffällt")
    for product in found[:5]:
        print(f"      * {product.title[:60]}")
    lowered = body.lower()
    for marker in ("just in", "neu", "vorbestell", "add to cart", "in den warenkorb"):
        if marker in lowered:
            print(f"    Text-Treffer  : {marker!r} kommt vor")
            break
    print(f"    Anfang        : {body[:160].strip()!r}\n")


async def probe(url: str) -> None:
    settings = get_settings()
    if not settings.scraper_api_key or not settings.brightdata_zone:
        print("SCRAPER_API_KEY und BRIGHTDATA_ZONE müssen in der .env stehen.")
        return
    print(f"URL : {url}")
    print(f"Zone: {settings.brightdata_zone}   Land: {settings.scraper_api_country}\n")

    # Same request the app makes, then variants that could rescue a JS-only page.
    await _attempt("wie die App (raw)", url, {})
    await _attempt("mit JS-Rendering", url, {"render": True})
    await _attempt("als Markdown", url, {"data_format": "markdown"})


def main() -> None:
    asyncio.run(probe(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL))


if __name__ == "__main__":
    main()
