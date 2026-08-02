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
import time

from app.db import get_sessionmaker
from app.models import Watch
from app.monitor.adapters.pokemon_center_queue import QUEUE_HOST_MARKER, QUEUE_PAGE_PHRASES
from app.monitor.base import PageResult
from app.monitor.registry import resolve_adapter

BODY_PREVIEW = 240
REPEAT_GAP_SECONDS = 10.0


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


def _verdict(adapter, page: PageResult) -> None:
    """What the adapter concludes — the line that actually decides pings.

    Showing only the raw fetch was misleading: a Pokémon Center check reported
    "HTTP 200, Warteschlange: keine Anzeichen", which reads like a healthy quiet
    store. The adapter's own verdict was "only a bot interstitial, blind to the
    queue". Without it the operator sees reassurance instead of the problem.
    """
    try:
        result = adapter.parse(page)
    except Exception as exc:
        print(f"    Urteil        : Parser-Fehler {type(exc).__name__}: {exc}\n")
        return
    print(f"    URTEIL        : {result.status.value}")
    if result.alert_title:
        print(f"    Alarm         : {result.alert_title}")
    if result.note:
        print(f"    Begründung    : {result.note}")
    if result.price is not None:
        print(f"    Preis         : {result.price} {result.currency or ''}")
    print()


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


async def repeat_watch(watch_id: int, times: int) -> None:
    """Run the watch's own check `times` over and rate it.

    One green check proves the route can work, not that it works. The Pokémon
    Center unlocker was measured turning us away on roughly one attempt in
    three, and for an alarm the question is the rate, not a single sample.
    """
    async with get_sessionmaker()() as session:
        watch = await session.get(Watch, watch_id)
        if watch is None:
            print(f"Watch {watch_id} gibt es nicht.")
            return
        url, adapter_slug, label = watch.url, watch.adapter, watch.label
        detection = dict(watch.detection or {})

    print(f"Watch {watch_id}: {label}")
    print(f"{times} Versuche, je 10 s Pause — das dauert ein paar Minuten.")
    print("=" * 64)

    adapter = resolve_adapter(url, adapter_slug, detection)
    good = 0
    for attempt in range(1, times + 1):
        if attempt > 1:
            await asyncio.sleep(REPEAT_GAP_SECONDS)
        started = time.monotonic()
        try:
            page = await adapter.fetch(url)
            result = adapter.parse(page)
            good += 1
            print(
                f"{attempt:>3}. OK      HTTP {page.status_code}  "
                f"{len(page.text):>7} Zeichen  {page.elapsed_ms / 1000:5.1f} s  "
                f"-> {result.status.value}"
                + (f"  {result.alert_title}" if result.alert_title else "")
            )
        except Exception as exc:
            # Full text, not a slice: a cut-off reason cost two rounds of wrong
            # diagnosis here already ("x-brd-error=Tim" hid "Timeout"). The
            # duration matters just as much — a fast failure and a slow one have
            # nothing in common.
            elapsed = time.monotonic() - started
            print(f"{attempt:>3}. FEHLER  nach {elapsed:5.1f} s  {type(exc).__name__}: {exc}")

    print("=" * 64)
    rate = good / times if times else 0
    print(f"{good} von {times} Versuchen erfolgreich ({rate:.0%}).")
    if good == times:
        print("Der Abruf ist stabil.")
    elif good:
        # What matters for an alarm is not a single miss but a run of them. With
        # a 3-minute interval, the blind window is (1-rate)^n * 3 minutes.
        blind = (1 - rate) ** 3
        print(
            f"Der Tracker prüft alle 3 Minuten. Dass drei Prüfungen hintereinander\n"
            f"scheitern — also 9 Minuten blind — passiert bei dieser Quote in {blind:.0%}\n"
            f"der Fälle. Eine Pokémon-Center-Warteschlange läuft deutlich länger."
        )
    else:
        print("Kein einziger Versuch kam durch — hier stimmt etwas nicht, schick mir die Ausgabe.")


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
        _verdict(adapter, page)
    except Exception as exc:
        _report("So prüft der Tracker gerade", None, f"{type(exc).__name__}: {exc}")

    if detection.get("fetcher") != "playwright":
        try:
            page = await fetchers.fetch_playwright(url)
            _report("Zum Vergleich: echter Browser (Playwright)", page, None)
        except Exception as exc:
            _report(
                "Zum Vergleich: echter Browser (Playwright)", None, f"{type(exc).__name__}: {exc}"
            )
        print(
            "Wenn der Browser eine deutlich größere Seite oder HTTP 200 liefert, sieht er\n"
            'mehr als der einfache Abruf. Dann in der Watch "Abruf-Methode" auf\n'
            '"Echter Browser" stellen. Kommt auch der nicht durch: "Unlocker".'
        )


async def list_watches(needle: str | None = None) -> None:
    """Print id + label for every watch, optionally filtered.

    Without this the tool needed an id the operator had to look up first, and a
    placeholder like <ID> or NEUE_ID in an instruction gets pasted literally —
    it happened twice.

    A search also checks the first match. "All the watches on this shop are
    broken" is one question, and answering it should not need a second command
    with a number copied out of the first one's output.
    """
    from sqlalchemy import select

    async with get_sessionmaker()() as session:
        watches = list((await session.scalars(select(Watch).order_by(Watch.id))).all())
    if needle:
        lowered = needle.lower()
        watches = [w for w in watches if lowered in w.label.lower() or lowered in w.url.lower()]
    if not watches:
        print("Keine passende Watch gefunden." if needle else "Es gibt noch keine Watches.")
        return
    print(f"{'ID':>4}  {'Label':32}  Adapter")
    for w in watches:
        state = "" if w.enabled else "  (aus)"
        print(f"{w.id:>4}  {w.label[:32]:32}  {w.adapter or '(automatisch)'}{state}")
    print()
    if not needle:
        print("Prüfen mit:  python -m app.watch_debug <ID>")
        return
    first = watches[0]
    print(f"Ich prüfe stellvertretend die erste ({first.id}):")
    print()
    await debug_watch(first.id)
    if len(watches) > 1:
        print(f"\nDie anderen {len(watches) - 1} laufen über denselben Shop — gleiche Ursache.")


async def _run(target: int | str | None, times: int) -> None:
    from app.monitor.fetchers import close_client, shutdown_playwright

    try:
        if not isinstance(target, int):
            await list_watches(target)
        elif times > 1:
            await repeat_watch(target, times)
        else:
            await debug_watch(target)
    finally:
        await shutdown_playwright()
        await close_client()


def main() -> None:
    """No argument lists the watches; a number debugs one; text searches.

    A second number repeats the check that many times and reports the success
    rate — "python -m app.watch_debug 63 5".
    """
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    target: int | str | None = int(arg) if arg and arg.isdigit() else arg
    second = sys.argv[2] if len(sys.argv) > 2 else ""
    asyncio.run(_run(target, int(second) if second.isdigit() else 1))


if __name__ == "__main__":
    main()
