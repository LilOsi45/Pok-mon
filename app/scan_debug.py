"""Explain what a scanner actually sees — why it does (not) find products.

Runs the exact same fetch + parse + keyword path as ``run_scan``, but prints a
report instead of touching the database or sending anything:

    docker compose exec -T app python -m app.scan_debug 1

Answers the two questions a silent scanner raises: did the page even yield
products, and if so which keyword filtered them out?
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.db import get_sessionmaker
from app.models import ProductScan, ScanItem
from app.scanner import normalize_text, parse_listing, url_key

SAMPLE = 12


def _explain(title: str, keywords: list[str], excludes: list[str]) -> str:
    """Why this product did (not) survive the keyword filter."""
    text = normalize_text(title)
    missing = [k for k in keywords if k.strip() and normalize_text(k) not in text]
    if missing:
        return "fehlt: " + ", ".join(repr(m) for m in missing)
    hit = [x for x in excludes if x.strip() and normalize_text(x) in text]
    if hit:
        return "ausgeschlossen durch: " + ", ".join(repr(h) for h in hit)
    return "TREFFER"


async def debug_scan(scan_id: int) -> None:
    from app.monitor import fetchers
    from app.monitor.registry import resolve_adapter

    async with get_sessionmaker()() as session:
        scan = await session.get(ProductScan, scan_id)
        if scan is None:
            print(f"Scanner {scan_id} gibt es nicht.")
            return
        keywords = list(scan.keywords or [])
        excludes = list(scan.exclude_keywords or [])
        known = set(
            (
                await session.scalars(select(ScanItem.url_key).where(ScanItem.scan_id == scan_id))
            ).all()
        )

    print(f"Scanner {scan.id}: {scan.label}")
    print(f"URL            : {scan.url}")
    print(f"Schlagwörter   : {keywords}  (ALLE müssen vorkommen)")
    print(f"Ausschlüsse    : {excludes}")
    print(f"Channels       : {list(scan.channels or [])}")
    print(f"Baseline erledigt: {bool(scan.baseline_done)}  |  bekannte Produkte: {len(known)}")
    print("-" * 60)

    adapter = resolve_adapter(scan.url)
    try:
        if adapter.fetcher == "scraperapi":
            via = "Bright Data / Unlocker"
            page = await fetchers.fetch_scraperapi(scan.url)
        elif scan.use_playwright:
            via = "Playwright (Browser)"
            page = await fetchers.fetch_playwright(scan.url)
        else:
            via = "httpx (direkt)"
            page = await fetchers.fetch_httpx(scan.url)
    except Exception as exc:
        print(f"ABRUF FEHLGESCHLAGEN ({type(exc).__name__}): {exc}")
        return

    print(f"Abgerufen über : {via}")
    print(f"HTTP           : {page.status_code}   ({len(page.text)} Zeichen)")
    if page.final_url != scan.url:
        print(f"Weitergeleitet : {page.final_url}")
    if page.status_code != 200:
        print("\n=> Der Scanner bricht hier ab: alles außer HTTP 200 gilt als Fehler.")
        return

    if not page.text.strip():
        print(
            "\n=> Die Antwort war LEER (0 Zeichen). Das ist KEIN Parser-Problem — der\n"
            "   Abruf selbst hat nichts geliefert. Bei Bright Data ist der HTTP-Code\n"
            "   oben der Status der API, nicht der Zielseite: ein leerer Körper heißt,\n"
            "   der Unlocker kam nicht an die Seite."
        )
        return

    found = parse_listing(page.text, page.final_url)
    print(f"Produkte erkannt: {len(found)}")
    if not found:
        print(
            "\n=> Die Seite gibt KEINE Produkte her. Meist heißt das: Der Shop baut die\n"
            "   Liste per JavaScript zusammen. Abhilfe: im Scanner 'Browser-Modus'\n"
            "   anhaken — oder eine URL nehmen, die die Produkte direkt ausliefert."
        )
        return

    verdicts = [(p, _explain(p.title, keywords, excludes)) for p in found]
    hits = [p for p, v in verdicts if v == "TREFFER"]
    new = [p for p in hits if url_key(p.url) not in known]
    print(f"Davon Treffer   : {len(hits)}")
    print(f"Davon NEU       : {len(new)}   <- nur diese würden pingen")
    print("-" * 60)
    for product, verdict in verdicts[:SAMPLE]:
        mark = "OK " if verdict == "TREFFER" else "-- "
        print(f"{mark}{product.title[:62]}")
        if verdict != "TREFFER":
            print(f"     {verdict}")
    if len(verdicts) > SAMPLE:
        print(f"... und {len(verdicts) - SAMPLE} weitere")

    if hits and not new:
        print(
            "\n=> Alles schon bekannt. Der Scanner arbeitet korrekt und meldet sich,\n"
            "   sobald ein WIRKLICH neues Produkt auftaucht."
        )
    elif not hits:
        print(
            "\n=> Produkte da, aber kein einziger Treffer. Schau oben, welches\n"
            "   Schlagwort fehlt — Komma bedeutet UND, nicht ODER."
        )


async def list_scans(needle: str | None = None) -> None:
    """Print id + label for every scanner, optionally filtered by name or URL.

    Demanding an id and answering "Aufruf: ... <scanner-id>" is what made the
    same instruction unusable for watches: a placeholder in an instruction gets
    pasted literally. Naming the shop has to be enough to find it.
    """
    async with get_sessionmaker()() as session:
        scans = list((await session.scalars(select(ProductScan).order_by(ProductScan.id))).all())
    if needle:
        lowered = needle.lower()
        scans = [s for s in scans if lowered in s.label.lower() or lowered in s.url.lower()]
    if not scans:
        print("Kein passender Scanner gefunden." if needle else "Es gibt noch keine Scanner.")
        return
    print(f"{'ID':>4}  {'Name':32}  URL")
    for s in scans:
        state = "" if s.enabled else "  (aus)"
        print(f"{s.id:>4}  {s.label[:32]:32}  {s.url[:60]}{state}")
    print()
    print("Prüfen mit:  python -m app.scan_debug <ID>")


async def _run(target: int | str | None) -> None:
    """Report, then shut the fetchers down — an un-closed Playwright subprocess
    spews 'Event loop is closed' tracebacks at interpreter exit."""
    from app.monitor.fetchers import close_client, shutdown_playwright

    try:
        if isinstance(target, int):
            await debug_scan(target)
        else:
            await list_scans(target)
    finally:
        await shutdown_playwright()
        await close_client()


def main() -> None:
    """No argument lists the scanners; a number debugs one; text searches."""
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(_run(int(arg) if arg and arg.isdigit() else arg))


if __name__ == "__main__":
    main()
