"""What the current watches/scanners cost and how much load they create.

    docker compose exec -T app python -m app.cost_report

Groups everything enabled by shop and shows which fetch path it uses. Only the
unlocker costs money, and it is easy to trigger by accident: a watch on a
bot-protected chain routes through it even with an empty detection config, so a
120 s interval quietly turns into ~30 $/month for that single watch.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from sqlalchemy import select

from app.db import get_sessionmaker
from app.models import ProductScan, Watch
from app.monitor.registry import resolve_adapter

# Bright Data Web Unlocker, pay as you go.
COST_PER_1000_REQUESTS = 1.50
SECONDS_PER_DAY = 86400
DAYS_PER_MONTH = 30
PAID_FETCHERS = {"scraperapi"}


@dataclass
class ShopLoad:
    domain: str
    adapter: str
    fetcher: str
    items: int = 0
    intervals: list[int] = field(default_factory=list)

    @property
    def requests_per_day(self) -> float:
        return sum(SECONDS_PER_DAY / max(iv, 1) for iv in self.intervals)

    @property
    def monthly_cost(self) -> float:
        if self.fetcher not in PAID_FETCHERS:
            return 0.0
        return self.requests_per_day * DAYS_PER_MONTH * COST_PER_1000_REQUESTS / 1000

    @property
    def fastest(self) -> int:
        return min(self.intervals) if self.intervals else 0


def _domain(url: str) -> str:
    parts = url.split("/")
    return parts[2] if len(parts) > 2 else url


def collect(watches: list[Watch], scans: list[ProductScan]) -> list[ShopLoad]:
    """Group enabled watches and scanners by shop + fetch path."""
    shops: dict[tuple[str, str, str], ShopLoad] = {}
    for item in [*watches, *scans]:
        adapter = resolve_adapter(
            item.url,
            getattr(item, "adapter", None),
            dict(getattr(item, "detection", None) or {}),
        )
        key = (_domain(item.url), adapter.slug, adapter.fetcher)
        load = shops.setdefault(key, ShopLoad(*key))
        load.items += 1
        load.intervals.append(item.interval_seconds)
    return sorted(shops.values(), key=lambda s: (-s.monthly_cost, -s.requests_per_day))


def render(loads: list[ShopLoad]) -> str:
    lines = [
        f"{'Anz':>4}  {'Shop':28} {'Abrufweg':12} {'schnellst':>10} {'Abrufe/Tag':>11}  Kosten",
        "-" * 88,
    ]
    for load in loads:
        cost = f"~{load.monthly_cost:6.2f} $/Monat" if load.monthly_cost else "gratis"
        lines.append(
            f"{load.items:4d}  {load.domain[:28]:28} {load.fetcher:12} "
            f"{load.fastest:9d}s {load.requests_per_day:11.0f}  {cost}"
        )
    total = sum(load.monthly_cost for load in loads)
    lines.append("-" * 88)
    lines.append(f"Summe kostenpflichtig: ~{total:.2f} $/Monat")
    if total:
        lines.append(
            "\nTipp: Das Intervall zu verdoppeln halbiert die Kosten. Für Vorbestellungen\n"
            "und Kategorieseiten reichen 900–1800 s fast immer."
        )
    return "\n".join(lines)


async def report() -> str:
    from app.config import get_settings
    from app.retention import row_counts

    async with get_sessionmaker()() as session:
        watches = list((await session.scalars(select(Watch).where(Watch.enabled))).all())
        scans = list((await session.scalars(select(ProductScan).where(ProductScan.enabled))).all())
        counts = await row_counts(session)

    settings = get_settings()
    history = (
        f"\nVerlauf in der Datenbank: {counts['stock_checks']:,} Checks, "
        f"{counts['notifications']:,} Benachrichtigungen\n"
        f"Wird nachts aufgeräumt: Checks älter als {settings.check_retention_days} Tage, "
        f"Benachrichtigungen älter als {settings.notification_retention_days} Tage."
    ).replace(",", ".")
    return render(collect(watches, scans)) + "\n" + history


def main() -> None:
    print(asyncio.run(report()))


if __name__ == "__main__":
    main()
