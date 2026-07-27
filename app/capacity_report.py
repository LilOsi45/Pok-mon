"""Can each shop actually be checked as often as its watches ask for?

    docker compose exec -T app python -m app.capacity_report

Two politeness rules cap how fast one shop can be polled: only one request at a
time per domain (PER_DOMAIN_CONCURRENCY) and a minimum gap between them
(PER_DOMAIN_MIN_INTERVAL_SECONDS). With 18 watches on one shop and a 20 s gap,
that shop can serve 3 checks per minute while the schedule asks for 9 — so two
thirds of the checks queue up, run past their own interval and get dropped by
the scheduler. The dashboard then shows "VERALTET" and blames nothing in
particular.

The way out is not a longer interval: Shopify shops publish their whole
catalogue at /products.json, and one such request serves *every* watch on that
shop. This report shows which watches that catalogue already covers and which
still need a request of their own — the latter are what compete for the budget.

The numbers come from the running app over localhost, not from the shops. An
earlier version probed each domain itself, from a process with none of the
app's cooldown state, so every run sent fresh requests to all 15 shops and
re-armed the rate limit it was meant to observe.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy import select

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import ProductScan, Watch

BAR_WIDTH = 34


@dataclass
class Domain:
    host: str
    #  (label, interval, is_catalog_candidate)
    items: list[tuple[str, int, bool]] = field(default_factory=list)
    catalog: dict[str, dict] | None = None
    probe_error: str | None = None

    def _is_served(self, url: str, candidate: bool) -> bool:
        """True when the shared catalogue answers this URL without a own request."""
        if not candidate or not self.catalog:
            return False
        from app.monitor.catalog import product_handle

        return product_handle(url) in self.catalog

    @property
    def served_by_catalog(self) -> list[tuple[str, int, bool]]:
        return [item for item in self.items if self._is_served(item[0], item[2])]

    @property
    def own_request(self) -> list[tuple[str, int, bool]]:
        return [item for item in self.items if not self._is_served(item[0], item[2])]

    @property
    def demand_per_min(self) -> float:
        """Requests/min the shop must answer with the catalogue in play."""
        settings = get_settings()
        own = sum(60.0 / interval for _url, interval, _c in self.own_request if interval > 0)
        catalog_cost = 60.0 / settings.catalog_ttl_seconds if self.served_by_catalog else 0.0
        return own + catalog_cost

    @property
    def supply_per_min(self) -> float:
        settings = get_settings()
        gap = max(settings.per_domain_min_interval_seconds, 0.01)
        return (60.0 / gap) * max(settings.per_domain_concurrency, 1)

    @property
    def verdict(self) -> str:
        # A failed probe only means "no catalogue" — judge on the numbers either way.
        if self.demand_per_min <= self.supply_per_min:
            return "OK"
        if self.demand_per_min <= self.supply_per_min * 1.5:
            return "KNAPP"
        return "ÜBERLASTET"


APP_URL = "http://127.0.0.1:8000/healthz/shops"


async def _live_state() -> tuple[dict[str, dict], str | None]:
    """Ask the running app what it knows, instead of asking the shops.

    Probing from here sent a fresh request to all 15 shops on every run, from a
    process with none of the app's cooldown state — so the diagnostic kept
    re-arming the very rate limit it was supposed to observe.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(APP_URL)
        resp.raise_for_status()
        return resp.json().get("shops", {}), None
    except Exception as exc:
        return {}, f"App nicht erreichbar ({type(exc).__name__}) — läuft der Container?"


def _apply(domain: Domain, shops: dict[str, dict]) -> None:
    # The app keys by netloc, which may still carry a www prefix.
    state = shops.get(domain.host) or shops.get(f"www.{domain.host}") or {}
    cat = state.get("catalog") or {}
    cooldown = state.get("cooldown") or {}

    handles = cat.get("handles") or []
    domain.catalog = {handle: {} for handle in handles} or None
    domain.probe_error = cat.get("reason")

    remaining = cooldown.get("remaining_seconds") or 0
    refusals = cooldown.get("consecutive_refusals") or 0
    if remaining:
        domain.probe_error = f"Pause {remaining:.0f}s, {refusals}x abgewiesen"
    elif not state:
        domain.probe_error = "noch nicht geprüft"


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


async def collect() -> list[Domain]:
    settings = get_settings()
    async with get_sessionmaker()() as session:
        watches = list((await session.scalars(select(Watch).where(Watch.enabled))).all())
        scans = list((await session.scalars(select(ProductScan).where(ProductScan.enabled))).all())

    grouped: dict[str, Domain] = defaultdict(lambda: Domain(host=""))
    for w in watches:
        host = _host(w.url)
        domain = grouped[host]
        domain.host = host
        candidate = settings.use_shop_catalog and "/products/" in w.url
        domain.items.append((w.url, w.interval_seconds, candidate))
    for s in scans:
        # A scanner always fetches its collection page itself: it has to see
        # products that no watch knows about yet.
        host = _host(s.url)
        domain = grouped[host]
        domain.host = host
        domain.items.append((s.url, s.interval_seconds, False))

    domains = sorted(grouped.values(), key=lambda d: -len(d.items))
    shops, error = await _live_state()
    for domain in domains:
        if error:
            domain.probe_error = error
        else:
            _apply(domain, shops)
    return domains


def render(domains: list[Domain]) -> str:
    if not domains:
        return "Keine aktiven Watches oder Scanner."
    settings = get_settings()
    out = [
        f"Pro-Shop-Budget: {settings.per_domain_min_interval_seconds:.0f}s Abstand, "
        f"{settings.per_domain_concurrency} gleichzeitig "
        f"= {domains[0].supply_per_min:.1f} Abrufe/min",
        "",
    ]

    problems = [d for d in domains if d.verdict != "OK"]
    out.append(f"{len(domains) - len(problems)} von {len(domains)} Shops im Budget")
    out.append("")
    out.append(f"{'Shop':26} {'Watch':>5} {'Katalog':>7} {'Soll':>6} {'Kann':>6}  Urteil")
    for d in domains:
        served = len(d.served_by_catalog)
        out.append(
            f"{d.host[:26]:26} {len(d.items):5d} {served:7d} "
            f"{d.demand_per_min:6.1f} {d.supply_per_min:6.1f}  {d.verdict}"
        )

    # A shop inside its budget but without a catalogue is still the expensive
    # case: every product costs its own request, and that is what triggers 429.
    costly = sorted(
        (d for d in domains if len(d.own_request) > 2),
        key=lambda d: -len(d.own_request),
    )
    if costly:
        out.append("")
        out.append("Viele Einzelabrufe statt einem Katalog-Abruf:")
        for d in costly:
            out.append(f"  {d.host[:24]:24} {len(d.own_request):3d}x  {d.probe_error or '—'}")

    if problems:
        out.append("")
        out.append(
            "ÜBERLASTET heißt: Der Shop schafft die Intervalle nicht. Ein Intervall,\n"
            "das er nicht schafft, prüft seltener als ein längeres, das passt."
        )
    return "\n".join(out)


async def report() -> str:
    return render(await collect())


def main() -> None:
    print(asyncio.run(report()))


if __name__ == "__main__":
    main()
