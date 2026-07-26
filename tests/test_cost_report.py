"""Cost report: a paid fetch path must never hide behind an empty detection."""

from __future__ import annotations

from app.cost_report import collect, render
from app.models import Game, ProductScan, Watch


def watch(url: str, interval: int = 120, **kw) -> Watch:
    return Watch(
        game=Game.POKEMON, label="w", url=url, interval_seconds=interval, enabled=True, **kw
    )


def scan(url: str, interval: int = 900) -> ProductScan:
    return ProductScan(
        game=Game.POKEMON,
        label="s",
        url=url,
        keywords=["x"],
        interval_seconds=interval,
        enabled=True,
    )


class TestCollect:
    def test_free_shop_costs_nothing(self):
        loads = collect([watch("https://cardsrfun.de/products/a")], [])
        assert len(loads) == 1
        assert loads[0].fetcher != "scraperapi"
        assert loads[0].monthly_cost == 0

    def test_unlocker_shop_is_priced_even_with_empty_detection(self):
        """The trap: detection={} still routes a chain through the unlocker."""
        loads = collect([watch("https://www.pokemoncenter.com/en-gb/p/1", detection={})], [])
        assert loads[0].fetcher == "scraperapi"
        # 120 s -> 720 requests/day -> 21600/month -> 1.50 $ per 1000
        assert round(loads[0].monthly_cost, 2) == 32.40

    def test_doubling_the_interval_halves_the_cost(self):
        fast = collect([watch("https://www.pokemoncenter.com/p", 120)], [])[0]
        slow = collect([watch("https://www.pokemoncenter.com/p", 240)], [])[0]
        assert round(fast.monthly_cost / slow.monthly_cost, 2) == 2.0

    def test_same_shop_is_grouped_and_intervals_summed(self):
        loads = collect(
            [watch("https://www.pokemoncenter.com/a", 120), watch("https://www.pokemoncenter.com/b", 600)],
            [],
        )
        assert len(loads) == 1
        assert loads[0].items == 2
        assert loads[0].fastest == 120
        assert round(loads[0].requests_per_day) == 720 + 144

    def test_scanners_are_counted_too(self):
        loads = collect([], [scan("https://www.pokemoncenter.com/category/x")])
        assert loads[0].items == 1 and loads[0].monthly_cost > 0

    def test_most_expensive_shop_comes_first(self):
        loads = collect(
            [watch("https://cardsrfun.de/a"), watch("https://www.pokemoncenter.com/b")], []
        )
        assert loads[0].domain == "www.pokemoncenter.com"


class TestRender:
    def test_marks_free_and_paid_rows(self):
        out = render(
            collect(
                [watch("https://cardsrfun.de/a"), watch("https://www.pokemoncenter.com/b")], []
            )
        )
        assert "gratis" in out
        assert "$/Monat" in out
        assert "Summe kostenpflichtig" in out

    def test_no_paid_shop_means_no_tip(self):
        out = render(collect([watch("https://cardsrfun.de/a")], []))
        assert "Summe kostenpflichtig: ~0.00 $/Monat" in out
        assert "Tipp" not in out
