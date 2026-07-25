"""Show what a watch actually sees — and compare the plain and browser fetch.

    docker compose exec -T app python -m app.watch_debug 34

For a shop behind a JS challenge (Pokémon Center) the plain HTTP client only
ever gets the wall, so a drop looks exactly like a quiet minute. A real browser
can run the challenge and reach the page behind it. This prints both side by
side so the difference is a fact, not a guess.
"""

from __future__ import annotations

import asyncio
import sys

from app.db import get_sessionmaker
from app.models import Watch
from app.monitor.adapters.pokemon_center_queue import QUEUE_HOST_MARKER, QUEUE_PAGE_PHRASES
from app.monitor.base import PageResult
from app.monitor.registry import resolve_adapter

BODY_PREVIEW = 240


def _queue_signals(page: PageResult) -> list[str]:
    """Everything in the response that would indicate a live waiting room."""
    hits: list[str] = []
    body = page.text[:20000].lower()
    location = (page.headers.get("location") or "").lower()
    if QUEUE_HOST_MARKER in location:
        hits.append(f"Weiterleitung -> {location[:70]}")
    if QUEUE_HOST_MARKER in page.final_url.lower():
        hits.append(f"Ziel-URL ist die Warteschlange: {page.final_url[:70]}")
    for phrase in QUEUE_PAGE_PHRASES:
        if phrase in body:
            hits.append(f"Warteschlangen-Text: {phrase!r}")
    return hits


def _report(name: str, page: PageResult | None, error: str | None) -> None:
    print(f"--- {name}")
    if page is None:
        print(f"    FEHLER: {error}")
        print()
        return
    print(f"    HTTP          : {page.status_code}")
    print(f"    Größe         : {len(page.text)} Zeichen")
    print(f"    Dauer         : {page.elapsed_ms} ms")
    if page.final_url != page.url:
        print(f"    Gelandet auf  : {page.final_url}")
    server = page.headers.get("server") or page.headers.get("x-served-by")
    if server:
        print(f"    Server-Header : {server}")
    signals = _queue_signals(page)
    if signals:
        print("    WARTESCHLANGE ERKANNT:")
        for hit in signals:
            print(f"      * {hit}")
    else:
        print("    Warteschlange : keine Anzeichen")
    preview = " ".join(page.text.split())[:BODY_PREVIEW]
    print(f"    Anfang        : {preview}")
    print()


async def debug_watch(watch_id: int) -> None:
    from app.monitor import fetchers

    async with get_sessionmaker()() as session:
        watch = await session.get(Watch, watch_id)
        if watch is None:
            print(f"Watch {watch_id} gibt es nicht.")
            return
        url, adapter_slug = watch.url, watch.adapter
        detection = dict(watch.detection or {})
        label = watch.label

    print(f"Watch {watch_id}: {label}")
    print(f"URL     : {url}")
    print(f"Adapter : {adapter_slug or '(automatisch)'}")
    print(f"Detection: {detection or '{}'}")
    print("=" * 64)

    adapter = resolve_adapter(url, adapter_slug, detection)
    try:
        page = await adapter.fetch(url)
        _report(f"So prüft der Tracker gerade ({page.fetched_via})", page, None)
    except Exception as exc:
        _report("So prüft der Tracker gerade", None, f"{type(exc).__name__}: {exc}")

    if detection.get("fetcher") != "playwright":
        try:
            page = await fetchers.fetch_playwright(url)
            _report("Zum Vergleich: echter Browser (Playwright)", page, None)
        except Exception as exc:
            _report("Zum Vergleich: echter Browser (Playwright)", None, f"{type(exc).__name__}: {exc}")
        print(
            'Wenn der Browser eine deutlich größere Seite oder HTTP 200 liefert, sieht er\n'
            "mehr als der einfache Abruf. Dann lohnt sich in der Watch unter Detection:\n"
            '    {"fetcher": "playwright"}'
        )


async def _run(watch_id: int) -> None:
    from app.monitor.fetchers import close_client, shutdown_playwright

    try:
        await debug_watch(watch_id)
    finally:
        await shutdown_playwright()
        await close_client()


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print("Aufruf: python -m app.watch_debug <watch-id>")
        raise SystemExit(2)
    asyncio.run(_run(int(sys.argv[1])))


if __name__ == "__main__":
    main()
