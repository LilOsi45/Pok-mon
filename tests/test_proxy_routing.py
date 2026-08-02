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
    monkeypatch.setenv("PROXY_MODE", "on-refusal")
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


class TestAlwaysMode:
    """The operator bought a proxy to remove gaps, so by default everything uses it."""

    def _always(self, monkeypatch):
        monkeypatch.setenv("PROXY_MODE", "always")
        import app.config as config

        config.get_settings.cache_clear()

    def test_every_shop_goes_through_it(self, monkeypatch):
        self._always(monkeypatch)
        assert proxy.routes_via_proxy(SHOP, PAGE_BUCKET) is True
        assert proxy.routes_via_proxy("kartenmeier.de", CATALOG_BUCKET) is True

    def test_without_a_proxy_url_nothing_changes(self, monkeypatch):
        monkeypatch.delenv("PROXY_URL", raising=False)
        self._always(monkeypatch)
        assert proxy.routes_via_proxy(SHOP, PAGE_BUCKET) is False

    def test_a_spent_budget_falls_back_instead_of_failing(self, monkeypatch):
        """A used-up budget must slow us down, never take the tracker offline."""
        monkeypatch.setenv("PROXY_DAILY_MB_BUDGET", "1")
        self._always(monkeypatch)
        proxy.note_success(SHOP, PAGE_BUCKET, 2_000_000, was_proxied=True)

        assert proxy.routes_via_proxy(SHOP, PAGE_BUCKET) is False


class TestCooldownsFollowTheRoute:
    """Regression in my own design: with "always" every request would have
    skipped the cooldown check, disabling the rate-limit protection entirely.

    A refusal belongs to the address that earned it. Keying cooldowns by route
    keeps both halves honest: switching to the proxy is a genuine fresh start,
    and a proxy that gets refused is still made to wait.
    """

    def test_a_direct_refusal_does_not_bar_the_proxy(self):
        from app.monitor import fetchers

        fetchers.reset_cooldowns()
        fetchers.note_refusal(SHOP, 60.0, PAGE_BUCKET, fetchers.DIRECT_ROUTE)

        assert fetchers.cooldown_remaining(SHOP, PAGE_BUCKET, fetchers.DIRECT_ROUTE) > 0
        assert fetchers.cooldown_remaining(SHOP, PAGE_BUCKET, fetchers.PROXY_ROUTE) == 0
        fetchers.reset_cooldowns()

    def test_a_refused_proxy_still_has_to_wait(self):
        """Otherwise "always" would mean "never back off"."""
        from app.monitor import fetchers

        fetchers.reset_cooldowns()
        fetchers.note_refusal(SHOP, 60.0, PAGE_BUCKET, fetchers.PROXY_ROUTE)

        assert fetchers.cooldown_remaining(SHOP, PAGE_BUCKET, fetchers.PROXY_ROUTE) > 0
        fetchers.reset_cooldowns()

    def test_repeated_proxy_refusals_still_back_off(self):
        from app.monitor import fetchers

        fetchers.reset_cooldowns()
        waits = [
            fetchers.note_refusal(SHOP, 60.0, PAGE_BUCKET, fetchers.PROXY_ROUTE) for _ in range(3)
        ]
        assert waits == [60.0, 120.0, 240.0]
        fetchers.reset_cooldowns()


class TestOnRefusalMode:
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

    def test_always_mode_does_not_claim_the_proxy_is_idle(self, monkeypatch):
        """Regression: "always" escalates nothing, so listing escalations alone
        reported "nothing uses the proxy" while every request went through it."""
        monkeypatch.setenv("PROXY_MODE", "always")
        import app.config as config

        config.get_settings.cache_clear()

        from app.proxy_report import render

        text = render({"proxy": proxy.report()})

        assert "alle Shops laufen über den Proxy" in text
        assert "Nichts läuft über den Proxy" not in text

    def test_it_says_so_when_no_proxy_is_configured(self, monkeypatch):
        monkeypatch.delenv("PROXY_URL", raising=False)
        import app.config as config

        config.get_settings.cache_clear()

        from app.proxy_report import render

        assert "Kein Proxy" in render({"proxy": proxy.report()})


class TestProjection:
    """Multiplying a partial day by 30 is not a forecast.

    Half an hour of measurement reported "1.50 $/month" for traffic that was
    actually running at roughly 15 GB a month — well past a 10 GB allowance.
    The rate has to be projected from the time actually measured.
    """

    def test_it_projects_from_the_observed_rate(self):
        import time

        proxy._state.started = time.monotonic() - 3600  # one hour of data
        proxy.note_success(SHOP, PAGE_BUCKET, 20_000_000, was_proxied=True)

        # 20 MB/h -> 480 MB/day -> ~14.4 GB/month
        assert proxy.report()["projection"]["gigabytes_per_month"] == pytest.approx(14.4, abs=0.2)

    def test_a_partial_day_is_not_multiplied_by_thirty(self):
        import time

        proxy._state.started = time.monotonic() - 1800  # half an hour
        proxy.note_success(SHOP, PAGE_BUCKET, 10_650_000, was_proxied=True)

        gb = proxy.report()["projection"]["gigabytes_per_month"]
        assert gb > 10, f"projected only {gb} GB — the old maths said 0.3"

    def test_the_measured_window_is_reported(self):
        import time

        proxy._state.started = time.monotonic() - 600
        assert proxy.report()["measured_minutes"] == pytest.approx(10, abs=0.5)

    def test_a_fresh_start_does_not_divide_by_zero(self):
        assert proxy.report()["projection"]["gigabytes_per_month"] == 0

    def test_the_report_warns_while_the_window_is_short(self):
        from app.proxy_report import render

        text = render({"proxy": proxy.report()})
        assert "wenig Messzeit" in text


class TestUrlNormalisation:
    """Providers hand out `host:port:user:pass`, which is not a URL.

    Rearranging those four fields by hand put a session token where the port
    belongs. urlsplit then raised "Port could not be cast to integer" on every
    single request, and the health report showed 41 of 43 watches failing with
    errors — "all fetch attempts failed", "Katalog nicht erreichbar" — that
    named the shops rather than the real cause.
    """

    def test_the_provider_format_is_understood(self):
        assert (
            proxy.normalize_proxy_url("geo.birdproxies.com:7777:kd12345:secret")
            == "http://kd12345:secret@geo.birdproxies.com:7777"
        )

    def test_a_proper_url_is_left_alone(self):
        url = "http://kd12345:secret@geo.birdproxies.com:7777"
        assert proxy.normalize_proxy_url(url) == url

    def test_the_scheme_is_kept(self):
        assert proxy.normalize_proxy_url("https://u:p@host.de:8080") == "https://u:p@host.de:8080"

    def test_credentials_without_a_scheme_work(self):
        assert (
            proxy.normalize_proxy_url("kd12345:secret@geo.birdproxies.com:7777")
            == "http://kd12345:secret@geo.birdproxies.com:7777"
        )

    def test_the_field_order_does_not_matter(self):
        """The regression: my conversion assumed one order and got another."""
        expected = "http://kd12345:secret@geo.birdproxies.com:7777"
        assert proxy.normalize_proxy_url("kd12345:secret:geo.birdproxies.com:7777") == expected
        assert proxy.normalize_proxy_url("geo.birdproxies.com:7777:kd12345:secret") == expected

    def test_host_and_port_only(self):
        assert proxy.normalize_proxy_url("proxy.local:3128") == "http://proxy.local:3128"

    def test_empty_means_no_proxy(self):
        assert proxy.normalize_proxy_url(None) is None
        assert proxy.normalize_proxy_url("   ") is None

    def test_unparseable_disables_the_proxy_instead_of_breaking_every_fetch(self):
        assert proxy.normalize_proxy_url("nonsense") is None
        assert proxy.normalize_proxy_url("a:b:c") is None

    def test_a_missing_port_is_refused(self):
        assert proxy.normalize_proxy_url("host.de:user:pass:more") is None

    def test_a_password_with_a_dot_is_not_mistaken_for_the_host(self):
        """With an @ present the order is already unambiguous — never reorder."""
        url = "http://kd12345:a.b.c@geo.birdproxies.com:7777"
        assert proxy.normalize_proxy_url(url) == url

    def test_a_password_with_a_colon_survives(self):
        url = "http://kd12345:pa:ss@geo.birdproxies.com:7777"
        assert proxy.normalize_proxy_url(url) == url

    def test_the_real_birdproxies_line(self):
        assert (
            proxy.normalize_proxy_url("residential.birdproxies.com:7777:pool-p1-cc-de:geheim")
            == "http://pool-p1-cc-de:geheim@residential.birdproxies.com:7777"
        )

    def test_a_password_that_looks_like_the_port_still_works(self):
        """Matching on value rather than position dropped it and raised."""
        assert (
            proxy.normalize_proxy_url("residential.birdproxies.com:7777:user:7777")
            == "http://user:7777@residential.birdproxies.com:7777"
        )

    def test_an_at_form_without_a_numeric_port_is_refused(self):
        assert proxy.normalize_proxy_url("u:p@host.de:session") is None

    def test_configured_follows_the_normalised_value(self, monkeypatch):
        monkeypatch.setenv("PROXY_URL", "nonsense")
        import app.config as config

        config.get_settings.cache_clear()
        assert proxy.configured() is False


class TestPendingRefusalsAreVisible:
    """ "Nichts läuft über den Proxy — kein Shop hat uns abgewiesen."

    That line was printed while fantasyworld.be was answering 429 to three
    watches. Both halves were wrong in a way that matters: refusals had been
    counted, they just had not reached the escalation threshold yet, and the
    counter lives in memory so every deploy resets it. Without that on screen
    there is no way to tell "the shop is fine" from "the switch has not tripped
    yet".
    """

    def _report(self, monkeypatch, refusals):
        import app.config as config
        from app import proxy

        monkeypatch.setenv("PROXY_URL", "http://u:p@proxy.example:8000")
        monkeypatch.setenv("PROXY_MODE", "on-refusal")
        config.get_settings.cache_clear()
        proxy.reset()
        for _ in range(refusals):
            proxy.note_refusal("fantasyworld.be", "page", was_proxied=False)
        state = proxy.report()
        config.get_settings.cache_clear()
        proxy.reset()
        return state

    def test_a_counted_refusal_shows_up_before_it_escalates(self, monkeypatch):
        from app.proxy_report import render

        text = render({"proxy": self._report(monkeypatch, 1)})

        assert "fantasyworld.be/page" in text
        assert "1x" in text
        assert "kein Shop hat uns abgewiesen" not in text

    def test_the_reset_on_restart_is_stated(self, monkeypatch):
        from app.proxy_report import render

        text = render({"proxy": self._report(monkeypatch, 1)})

        assert "Neustart" in text and "PROXY_SHOPS" in text

    def test_an_escalated_shop_is_not_listed_as_pending(self, monkeypatch):
        from app.proxy_report import render

        state = self._report(monkeypatch, 2)
        assert state["via_proxy"] == ["fantasyworld.be/page"]
        assert state["refusals"] == {}
        assert "noch nicht umgestellt" not in render({"proxy": state})

    def test_a_quiet_setup_still_says_nothing_was_refused(self, monkeypatch):
        from app.proxy_report import render

        text = render({"proxy": self._report(monkeypatch, 0)})

        assert "kein Shop hat uns abgewiesen" in text


class TestTheRefusalNamesTheRoute:
    """A 429 direct and a 429 through the proxy need opposite conclusions.

    fantasyworld.be answered 429 both before and after being pinned to the
    proxy, and the message read identically either time — so there was no way
    to tell "try a proxy" from "a proxy will not help, the shop blocks
    residential addresses too".
    """

    def test_a_direct_refusal_says_direct(self, monkeypatch):
        from app.monitor import fetchers

        fetchers.reset_cooldowns() if hasattr(fetchers, "reset_cooldowns") else None
        fetchers.note_refusal("fantasyworld.be", 60, fetchers.PAGE_BUCKET, fetchers.DIRECT_ROUTE)
        try:
            fetchers._refuse_if_cooling(
                "fantasyworld.be", fetchers.PAGE_BUCKET, fetchers.DIRECT_ROUTE
            )
        except fetchers.RateLimited as exc:
            assert "direkt" in str(exc)
        else:
            raise AssertionError("expected RateLimited")
        fetchers._domain_blocked_until.clear()
        fetchers._domain_penalty.clear()

    def test_a_proxied_refusal_says_so(self, monkeypatch):
        from app.monitor import fetchers

        fetchers.note_refusal("fantasyworld.be", 60, fetchers.PAGE_BUCKET, fetchers.PROXY_ROUTE)
        try:
            fetchers._refuse_if_cooling(
                "fantasyworld.be", fetchers.PAGE_BUCKET, fetchers.PROXY_ROUTE
            )
        except fetchers.RateLimited as exc:
            assert "über den Proxy" in str(exc)
        else:
            raise AssertionError("expected RateLimited")
        fetchers._domain_blocked_until.clear()
        fetchers._domain_penalty.clear()
