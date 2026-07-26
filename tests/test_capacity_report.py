"""The per-domain budget is a hard ceiling — the report must show when it's blown.

18 watches on one shop with a 20 s politeness gap ask for 9 checks/min from a
shop that can serve 3. The scheduler then drops the late runs and every watch on
that shop reports "VERALTET" with no error to point at. This asserts the report
names that situation instead of leaving it invisible.
"""

from __future__ import annotations

import pytest

from app import capacity_report
from app.capacity_report import Domain, render


def _domain(host: str, count: int, interval: int, *, catalog: dict | None = None) -> Domain:
    items = [(f"https://{host}/products/p{i}", interval, True) for i in range(count)]
    return Domain(host=host, items=items, catalog=catalog)


def test_oversubscribed_domain_is_flagged(monkeypatch):
    monkeypatch.setenv("PER_DOMAIN_MIN_INTERVAL_SECONDS", "20")
    monkeypatch.setenv("USE_SHOP_CATALOG", "true")
    capacity_report.get_settings.cache_clear()

    d = _domain("shop.example", 18, 120)  # no catalogue -> 18 own requests

    assert d.supply_per_min == pytest.approx(3.0)
    assert d.demand_per_min == pytest.approx(9.0)
    assert d.verdict == "ÜBERLASTET"
    capacity_report.get_settings.cache_clear()


def test_catalog_collapses_the_demand(monkeypatch):
    """The whole point of the shared catalogue: 18 requests become one."""
    monkeypatch.setenv("PER_DOMAIN_MIN_INTERVAL_SECONDS", "20")
    monkeypatch.setenv("CATALOG_TTL_SECONDS", "45")
    capacity_report.get_settings.cache_clear()

    index = {f"p{i}": {"handle": f"p{i}"} for i in range(18)}
    d = _domain("shop.example", 18, 120, catalog=index)

    assert len(d.served_by_catalog) == 18
    assert d.own_request == []
    assert d.demand_per_min == pytest.approx(60 / 45)  # one catalogue refresh
    assert d.verdict == "OK"
    capacity_report.get_settings.cache_clear()


def test_products_missing_from_the_catalog_still_cost_a_request(monkeypatch):
    monkeypatch.setenv("PER_DOMAIN_MIN_INTERVAL_SECONDS", "20")
    capacity_report.get_settings.cache_clear()

    index = {"p0": {"handle": "p0"}}  # only one of the three is listed
    d = _domain("shop.example", 3, 120, catalog=index)

    assert len(d.served_by_catalog) == 1
    assert len(d.own_request) == 2
    capacity_report.get_settings.cache_clear()


def test_scanners_always_cost_their_own_request(monkeypatch):
    """A scanner must see products no watch knows about — no catalogue shortcut."""
    monkeypatch.setenv("PER_DOMAIN_MIN_INTERVAL_SECONDS", "20")
    capacity_report.get_settings.cache_clear()

    d = Domain(
        host="shop.example",
        items=[("https://shop.example/collections/new", 300, False)],
        catalog={"whatever": {"handle": "whatever"}},
    )

    assert d.own_request == d.items
    capacity_report.get_settings.cache_clear()


def test_render_lists_the_offender_and_stays_short():
    over = _domain("busy.example", 18, 120)
    fine = _domain("calm.example", 1, 300, catalog={"p0": {"handle": "p0"}})

    text = render([over, fine])

    assert "busy.example" in text
    assert "ÜBERLASTET" in text
    assert "18x eigener Abruf" in text
    assert len(text.splitlines()) < 25, "must fit a phone screen"


def test_render_survives_an_empty_install():
    assert "Keine" in render([])
