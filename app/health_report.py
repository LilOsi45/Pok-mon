"""Which watches and scanners are actually working right now.

    docker compose exec -T app python -m app.health_report

Reads the recorded state instead of re-fetching everything: instant, free, and
it shows exactly what the scheduler last saw. For fresh numbers press
"Alle prüfen" in the dashboard first, then run this.

Reports three different failures that look alike from the outside:
  * FEHLER  — the last check raised (shop down, blocked, no route)
  * UNKLAR  — fetched fine but the stock state could not be read
  * VERALTET — no check for far longer than the interval: the job is not running
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select

from app.db import get_sessionmaker
from app.models import ProductScan, StockStatus, Watch, utcnow

STALE_FACTOR = 4  # missed this many intervals -> the schedule is broken, not the shop
LABEL_WIDTH = 30
MAX_ROWS = 10  # a phone screen, not a wall of identical lines


@dataclass
class Row:
    kind: str
    id: int
    label: str
    state: str
    age_seconds: float | None
    error: str | None

    @property
    def age(self) -> str:
        if self.age_seconds is None:
            return "nie"
        seconds = int(self.age_seconds)
        if seconds < 90:
            return f"{seconds}s"
        if seconds < 5400:
            return f"{seconds // 60}m"
        return f"{seconds // 3600}h"


def _classify(last_check, last_error, status, interval: int) -> tuple[str, float | None]:
    age = None if last_check is None else (utcnow() - last_check).total_seconds()
    if last_check is None:
        return "NIE GEPRÜFT", None
    if age is not None and age > interval * STALE_FACTOR:
        return "VERALTET", age
    if last_error:
        return "FEHLER", age
    if status is not None and status == StockStatus.UNKNOWN:
        return "UNKLAR", age
    return "OK", age


def collect(watches: list[Watch], scans: list[ProductScan]) -> list[Row]:
    rows: list[Row] = []
    for w in watches:
        state, age = _classify(w.last_check_at, w.last_error, w.last_status, w.interval_seconds)
        rows.append(Row("Watch", w.id, w.label, state, age, w.last_error))
    for s in scans:
        state, age = _classify(s.last_check_at, s.last_error, None, s.interval_seconds)
        rows.append(Row("Scanner", s.id, s.label, state, age, s.last_error))
    order = {"FEHLER": 0, "VERALTET": 1, "NIE GEPRÜFT": 2, "UNKLAR": 3, "OK": 4}
    return sorted(rows, key=lambda r: (order.get(r.state, 9), r.id))


def render(rows: list[Row]) -> str:
    counts = Counter(row.state for row in rows)
    total = len(rows)
    ok = counts.get("OK", 0)
    out = [f"{ok} von {total} in Ordnung"]
    problems = [f"{n}x {state}" for state, n in counts.items() if state != "OK"]
    if problems:
        out.append("Probleme: " + ", ".join(problems))
    out.append("")

    broken = [row for row in rows if row.state != "OK"]
    if not broken:
        out.append("Alles läuft. Nichts zu tun.")
        return "\n".join(out)

    # Grouped causes first: with 18 watches on one dead shop the same line
    # repeats 18 times, which tells you nothing you can act on.
    reasons = Counter(
        (row.error or "kein Fehlertext")[:70] for row in broken if row.state == "FEHLER"
    )
    if reasons:
        out.append("Ursachen:")
        for reason, n in reasons.most_common(6):
            out.append(f"  {n:3d}x  {reason}")
        out.append("")

    out.append("Betroffen:")
    for row in broken[:MAX_ROWS]:
        name = f"{row.kind[0]}{row.id} {row.label}"[:LABEL_WIDTH]
        out.append(f"  {row.state:11} {row.age:>4}  {name}")
    if len(broken) > MAX_ROWS:
        out.append(f"  … und {len(broken) - MAX_ROWS} weitere")

    if any(row.state == "VERALTET" for row in broken):
        out.append(
            "\nVERALTET heißt: Der Shop ist nicht schuld — der Job läuft nicht.\n"
            "Meist hilft ein Neustart:  docker compose restart app"
        )
    return "\n".join(out)


async def report() -> str:
    async with get_sessionmaker()() as session:
        watches = list((await session.scalars(select(Watch).where(Watch.enabled))).all())
        scans = list((await session.scalars(select(ProductScan).where(ProductScan.enabled))).all())
    return render(collect(watches, scans))


def main() -> None:
    print(asyncio.run(report()))


if __name__ == "__main__":
    main()
