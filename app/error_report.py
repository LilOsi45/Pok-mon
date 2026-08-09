"""Where do the failed checks come from?

    docker compose exec -T app python -m app.error_report
    docker compose exec -T app python -m app.error_report 72     # last 72 hours

The daily summary reported 3846 failures out of 27904 checks and named exactly
one watch — because the watchdog only lists what has been broken for a whole
hour. Everything that fails and recovers, over and over, was invisible in the
aggregate: a shop that refuses one check in three costs a third of its watches'
coverage without ever showing up as broken.

Groups the recorded check history by shop and by cause. Reads the database
only — it contacts no shop and sends nothing.
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import select

from app.db import get_sessionmaker
from app.models import StockCheck, Watch, utcnow

DEFAULT_HOURS = 24
TOP_SHOPS = 12
TOP_REASONS = 6
# Enough to tell "429" from "503" and "empty body" apart, not so much that two
# failures on the same shop look like different problems because a URL differs.
REASON_CHARS = 60


@dataclass(frozen=True)
class Row:
    """One recorded check."""

    shop: str
    label: str
    url: str
    error: str | None


def _shop(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.") or "?"


def _reason(error: str) -> str:
    """Collapse an error to its kind, so counts group instead of scattering."""
    text = error.split(" for ")[0].split(" — ")[0].strip()
    return text[:REASON_CHARS] or "(kein Text)"


DEAD_LINK_SHARE = 0.9  # this much of a watch's checks answering 404 = dead link


def dead_links(rows: list[Row]) -> list[tuple[str, str, int]]:
    """(label, url, checks) for watches whose URL just answers 404.

    Two shops ran at a 100 % failure rate — 2727 requests a day against pages
    that are gone. That is the largest single number in the report, it earns
    rate limits on shops we still want, and it is fixed by editing or deleting
    a watch. Naming them is the difference between a statistic and an action.
    """
    per_watch: dict[str, tuple[str, int, int]] = {}
    for row in rows:
        label, checks, notfound = per_watch.get(row.url, (row.label, 0, 0))
        per_watch[row.url] = (label, checks + 1, notfound + (1 if _is_404(row.error) else 0))
    dead = [
        (label, url, checks)
        for url, (label, checks, notfound) in per_watch.items()
        if checks and notfound / checks >= DEAD_LINK_SHARE
    ]
    return sorted(dead, key=lambda item: -item[2])


def _is_404(error: str | None) -> bool:
    return bool(error) and "404" in error


def render(rows: list[Row], hours: int) -> str:
    total = len(rows)
    if not total:
        return f"Keine Prüfungen in den letzten {hours} Stunden aufgezeichnet.\n"

    failed = [row for row in rows if row.error]
    per_shop: dict[str, list[int]] = {}
    for row in rows:
        counts = per_shop.setdefault(row.shop, [0, 0])
        counts[0] += 1
        if row.error:
            counts[1] += 1

    out = [
        f"Letzte {hours} h: {total} Prüfungen, {len(failed)} Fehler ({len(failed) / total:.0%})",
        "",
        f"{'Shop':28} {'Prüfungen':>10} {'Fehler':>7} {'Quote':>6}",
    ]
    ranked = sorted(per_shop.items(), key=lambda kv: (-kv[1][1], kv[0]))
    for shop, (checks, errors) in ranked[:TOP_SHOPS]:
        if not errors:
            continue
        out.append(f"{shop[:28]:28} {checks:>10} {errors:>7} {errors / checks:>5.0%}")
    if not failed:
        out.append("  (keiner)")

    if failed:
        out += ["", "Häufigste Ursachen:"]
        for reason, n in Counter(_reason(row.error) for row in failed).most_common(TOP_REASONS):
            out.append(f"  {n:5d}x  {reason}")

    if dead := dead_links(rows):
        wasted = sum(checks for _l, _u, checks in dead)
        out += [
            "",
            f"TOTE LINKS — {len(dead)} Watches fragen eine Seite ab, die es nicht gibt",
            f"({wasted} vergebliche Abrufe in {hours} h; das provoziert Sperren bei Shops,",
            "die wir noch brauchen). Im Dashboard URL korrigieren oder Watch löschen:",
        ]
        for label, url, checks in dead[:TOP_SHOPS]:
            out.append(f"  {checks:5d}x  {label[:28]:28}  {url[:60]}")

    quiet = [shop for shop, (_c, e) in per_shop.items() if not e]
    out += ["", f"Ohne einen einzigen Fehler: {len(quiet)} von {len(per_shop)} Shops"]
    return "\n".join(out) + "\n"


async def report(hours: int = DEFAULT_HOURS) -> str:
    since = utcnow() - timedelta(hours=hours)
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(Watch.label, Watch.url, StockCheck.error)
            .join(Watch, Watch.id == StockCheck.watch_id)
            .where(StockCheck.checked_at >= since)
        )
        rows = [Row(_shop(url), label, url, error) for label, url, error in result.all()]
    return render(rows, hours)


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    print(asyncio.run(report(int(arg) if arg.isdigit() else DEFAULT_HOURS)), end="")


if __name__ == "__main__":
    main()
