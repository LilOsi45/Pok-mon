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


def _shop(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.") or "?"


def _reason(error: str) -> str:
    """Collapse an error to its kind, so counts group instead of scattering."""
    text = error.split(" for ")[0].split(" — ")[0].strip()
    return text[:REASON_CHARS] or "(kein Text)"


def render(rows: list[tuple[str, str | None]], hours: int) -> str:
    """rows: (shop, error or None) for every check in the window."""
    total = len(rows)
    if not total:
        return f"Keine Prüfungen in den letzten {hours} Stunden aufgezeichnet.\n"

    failed = [(shop, err) for shop, err in rows if err]
    per_shop: dict[str, list[int]] = {}
    for shop, err in rows:
        counts = per_shop.setdefault(shop, [0, 0])
        counts[0] += 1
        if err:
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
        for reason, n in Counter(_reason(err) for _shop_, err in failed).most_common(TOP_REASONS):
            out.append(f"  {n:5d}x  {reason}")

    quiet = [shop for shop, (_c, e) in per_shop.items() if not e]
    out += ["", f"Ohne einen einzigen Fehler: {len(quiet)} von {len(per_shop)} Shops"]
    return "\n".join(out) + "\n"


async def report(hours: int = DEFAULT_HOURS) -> str:
    since = utcnow() - timedelta(hours=hours)
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(Watch.url, StockCheck.error)
            .join(Watch, Watch.id == StockCheck.watch_id)
            .where(StockCheck.checked_at >= since)
        )
        rows = [(_shop(url), error) for url, error in result.all()]
    return render(rows, hours)


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    print(asyncio.run(report(int(arg) if arg.isdigit() else DEFAULT_HOURS)), end="")


if __name__ == "__main__":
    main()
