"""Remove watches whose URL only ever answers 404.

    docker compose exec -T app python -m app.watch_cleanup                 # nur anzeigen
    docker compose exec -T app python -m app.watch_cleanup wirklich-loeschen

Five watches were spending 2718 requests a day on pages that are gone, which is
about a tenth of all traffic and earns rate limits on shops we still need.
Deleting them one by one through the dashboard on a phone is the kind of chore
that does not get done.

Deleting is not reversible, so this never deletes on its own: without the
confirmation word it only lists what it would remove, and it only ever
considers watches that answered 404 on nearly every check in the window — an
occasional 404 during a restock never qualifies.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta

from sqlalchemy import delete, select

from app.db import get_sessionmaker
from app.error_report import DEFAULT_HOURS, Row, _shop, dead_links
from app.models import StockCheck, Watch, utcnow

CONFIRM = "wirklich-loeschen"


async def find_dead(hours: int = DEFAULT_HOURS) -> list[tuple[int, str, str, int]]:
    """(watch id, label, url, wasted checks) for every dead link in the window."""
    since = utcnow() - timedelta(hours=hours)
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(Watch.label, Watch.url, StockCheck.error)
            .join(Watch, Watch.id == StockCheck.watch_id)
            .where(StockCheck.checked_at >= since)
        )
        rows = [Row(_shop(url), label, url, error) for label, url, error in result.all()]
        dead = dead_links(rows)
        if not dead:
            return []
        urls = [url for _label, url, _checks in dead]
        # Every watch on that URL, not one of them. Two watches shared a dead
        # fantasyworld.be address; a dict keyed by URL kept only the last id, so
        # the cleanup removed one and the other kept spending 1308 requests a
        # day on the same missing page — the exact waste this is here to end.
        found: dict[str, list[tuple[int, str]]] = {}
        rows_by_url = await session.execute(
            select(Watch.url, Watch.id, Watch.label).where(Watch.url.in_(urls))
        )
        for url, watch_id, label in rows_by_url.all():
            found.setdefault(url, []).append((watch_id, label))
    return [
        (watch_id, label, url, checks)
        for _label, url, checks in dead
        for watch_id, label in found.get(url, [])
    ]


async def run(confirmed: bool, hours: int = DEFAULT_HOURS) -> str:
    dead = await find_dead(hours)
    if not dead:
        return "Keine toten Links gefunden — nichts zu löschen.\n"

    lines = [f"{len(dead)} Watches fragen eine Seite ab, die es nicht gibt:"]
    for watch_id, label, url, checks in dead:
        lines.append(f"  {watch_id:>4}  {label}  ({checks} vergebliche Abrufe)")
        lines.append(f"        {url}")
    lines.append("")

    if not confirmed:
        lines.append(f"Nichts gelöscht. Zum Löschen den Befehl nochmal mit  {CONFIRM}  aufrufen.")
        return "\n".join(lines) + "\n"

    async with get_sessionmaker()() as session:
        await session.execute(delete(Watch).where(Watch.id.in_([d[0] for d in dead])))
        await session.commit()
    lines.append(f"{len(dead)} Watches gelöscht.")
    lines.append("Danach einmal neu starten, damit die Zeitpläne verschwinden:")
    lines.append("  docker compose restart app")
    return "\n".join(lines) + "\n"


def main() -> None:
    confirmed = len(sys.argv) > 1 and sys.argv[1] == CONFIRM
    print(asyncio.run(run(confirmed)), end="")


if __name__ == "__main__":
    main()
