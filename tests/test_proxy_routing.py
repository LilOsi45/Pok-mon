"""Pay for the proxy only where it is actually needed.

Residential proxies bill per gigabyte, so routing everything through them is the
expensive mistake. On this install 12 of 15 shops answer the server's own IP
perfectly well, and even on the three that refuse us it is only /products.json
that gets turned away — the scanner's ordinary pages kept returning 200
throughout. Sending one catalogue request through the proxy also replaces 18
product-page requests, so the same data costs a fraction.

Hence: nothing is proxied until a shop has actually refused us, it goes back to
the free route as soon as the shop relents, and a daily budget caps the bill.
"""

from __future__ import annotations

import pytest

from app import proxy
from app.monitor.fetchers import CATALOG_BUCKET, PAGE_BUCKET

SHOP = "geeksheaven.de"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("PROXY_URL", "http://user:pw@proxy.example:8000")
    monkeypatch.setenv("PROXY_AFTER_REFUSALS", "2")
    monkeypatch.setenv("PROXY_SHOPS", "")
    monkeypatch.setenv("PROXY_DAILY_REQUEST_BUDGET", "5000")
    monkeypatch.setenv("PROXY_DAILY_MB_BUDGET", "300")
    import app.config as config

    config.get_settings.cache_clear()
    proxy.reset()
    yield
    proxy.reset()
    config.get_settings.cache_clear()


class TestNothingIsProxiedByDefault:
    def test_a_healthy_shop_stays_free(self):
        assert proxy.routes_via_proxy(SHOP, PAGE_BUCKET) is False

    def test_one_refusal_is_not_enough(self):
        proxy.note_refusal(SHOP, CATALOG_BUCKET, was_proxied=False)
        assert proxy.routes_via_proxy(SHOP, CATALOG_BUCKET) is False

    def test_without_a_proxy_url_nothing_escalates(self, monkeypatch):
        monkeypatch.delenv("PROXY_URL", raising=False)
        import app.config as config

        config.get_settings.cache_clear()
        for _ in range(5):
            proxy.note_refusal(SHOP, CATALOG_BUCKET, was_proxied=False)
        assert proxy.routes_via_proxy(SHOP, CATALOG_BUCKET) is False


class TestEscalation:
    def test_the_refused_bucket_moves_to_the_proxy(self):
        proxy.note_refusal(SHOP, CATALOG_BUCKET, was_proxied=False)
        assert proxy.note_refusal(SHOP, CATALOG_BUCKET, was_proxied=False) is True

        assert proxy.routes_via_proxy(SHOP, CATALOG_BUCKET) is True

    def test_only_that_bucket_moves(self):
        """Pages that work must keep using the free route."""
        for _ in range(3):
            proxy.note_refusal(SHOP, CATALOG_BUCKET, was_proxied=False)

        assert proxy.routes_via_proxy(SHOP, PAGE_BUCKET) is False

    def test_only_that_shop_moves(self):
        for _ in range(3):
            proxy.note_refusal(SHOP, CATALOG_BUCKET, was_proxied=False)

        assert proxy.routes_via_proxy("kartenmeier.de", CATALOG_BUCKET) is False

    def test_a_refusal_that_already_used_the_proxy_does_not_escalate(self):
        """Otherwise a shop that dislikes us either way just burns budget."""
        for _ in range(5):
            assert proxy.note_refusal(SHOP, CATALOG_BUCKET, was_proxied=True) is False

        assert proxy.routes_via_proxy(SHOP, CATALOG_BUCKET) is False

    def test_it_drops_back_to_the_free_route_when_the_shop_relents(self):
        for _ in range(2):
            proxy.note_refusal(SHOP, CATALOG_BUCKET, was_proxied=False)
        assert proxy.routes_via_proxy(SHOP, CATALOG_BUCKET) is True

        proxy.note_success(SHOP, CATALOG_BUCKET, 1000, was_proxied=False)

        assert proxy.routes_via_proxy(SHOP, CATALOG_BUCKET) is False


class TestPinnedShops:
    def test_a_pinned_shop_is_proxied_from_the_start(self, monkeypatch):
        monkeypatch.setenv("PROXY_SHOPS", "geeksheaven.de, collectemall.de")
        import app.config as config

        config.get_settings.cache_clear()

        assert proxy.routes_via_proxy(SHOP, PAGE_BUCKET) is True
        assert proxy.routes_via_proxy("collectemall.de", CATALOG_BUCKET) is True
        assert proxy.routes_via_proxy("kartenmeier.de", PAGE_BUCKET) is False

    def test_the_www_prefix_does_not_matter(self, monkeypatch):
        monkeypatch.setenv("PROXY_SHOPS", "www.geeksheaven.de")
        import app.config as config

        config.get_settings.cache_clear()
        assert proxy.routes_via_proxy(SHOP, PAGE_BUCKET) is True


class TestBudget:
    def _escalate(self) -> None:
        for _ in range(2):
            proxy.note_refusal(SHOP, CATALOG_BUCKET, was_proxied=False)

    def test_megabytes_are_counted(self):
        self._escalate()
        proxy.note_success(SHOP, CATALOG_BUCKET, 2_000_000, was_proxied=True)

        assert proxy.report()["today"]["megabytes"] == 2.0
        assert proxy.report()["today"]["requests"] == 1

    def test_the_free_route_costs_nothing(self):
        proxy.note_success(SHOP, PAGE_BUCKET, 5_000_000, was_proxied=False)
        assert proxy.report()["today"]["megabytes"] == 0

    def test_a_spent_megabyte_budget_stops_the_proxy(self, monkeypatch):
        monkeypatch.setenv("PROXY_DAILY_MB_BUDGET", "1")
        import app.config as config

        config.get_settings.cache_clear()
        self._escalate()
        proxy.note_success(SHOP, CATALOG_BUCKET, 2_000_000, was_proxied=True)

        assert proxy.routes_via_proxy(SHOP, CATALOG_BUCKET) is False

    def test_a_spent_request_budget_stops_the_proxy(self, monkeypatch):
        monkeypatch.setenv("PROXY_DAILY_REQUEST_BUDGET", "2")
        import app.config as config

        config.get_settings.cache_clear()
        self._escalate()
        for _ in range(2):
            proxy.note_success(SHOP, CATALOG_BUCKET, 1000, was_proxied=True)

        assert proxy.routes_via_proxy(SHOP, CATALOG_BUCKET) is False

    def test_the_estimate_uses_the_configured_price(self, monkeypatch):
        monkeypatch.setenv("PROXY_COST_PER_GB", "8")
        import app.config as config

        config.get_settings.cache_clear()
        self._escalate()
        proxy.note_success(SHOP, CATALOG_BUCKET, 500_000_000, was_proxied=True)

        assert proxy.report()["today"]["estimated_cost"] == pytest.approx(4.0)

    def test_a_new_day_starts_a_fresh_budget(self):
        from datetime import timedelta

        self._escalate()
        proxy.note_success(SHOP, CATALOG_BUCKET, 9_000_000, was_proxied=True)
        proxy._state.day -= timedelta(days=1)

        assert proxy.report()["today"]["megabytes"] == 0


class TestReport:
    def test_it_names_what_is_being_paid_for(self):
        for _ in range(2):
            proxy.note_refusal(SHOP, CATALOG_BUCKET, was_proxied=False)
        proxy.note_success(SHOP, CATALOG_BUCKET, 1_500_000, was_proxied=True)

        from app.proxy_report import render

        text = render({"proxy": proxy.report()})

        assert "geeksheaven.de/catalog" in text
        assert "1.5" in text

    def test_it_says_so_when_no_proxy_is_configured(self, monkeypatch):
        monkeypatch.delenv("PROXY_URL", raising=False)
        import app.config as config

        config.get_settings.cache_clear()

        from app.proxy_report import render

        assert "Kein Proxy" in render({"proxy": proxy.report()})
