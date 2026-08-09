"""3846 failures out of 27904 checks, and the report named one watch.

The watchdog only lists what has been broken for a whole hour, so everything
that fails and recovers repeatedly stayed invisible — and a shop refusing one
check in three costs a third of its watches' coverage without ever counting as
broken. This is the view that shows where the failures actually come from.
"""

from __future__ import annotations

from app.error_report import Row, _reason, _shop, dead_links, render

WINDOW = 24


def rows(*spec: tuple[str, str | None, int]) -> list[Row]:
    out: list[Row] = []
    for shop, error, count in spec:
        out.extend([Row(shop, f"Watch {shop}", f"https://{shop}/products/x", error)] * count)
    return out


def watch_rows(url: str, error: str | None, count: int, label: str = "W") -> list[Row]:
    return [Row(_shop(url), label, url, error)] * count


class TestShopAndReason:
    def test_the_shop_is_the_domain_without_www(self):
        assert _shop("https://www.elbenwald.de/pokemon/x") == "elbenwald.de"

    def test_the_reason_drops_the_url_so_counts_group(self):
        a = _reason("brightdata returned an empty body for https://a.de/x")
        b = _reason("brightdata returned an empty body for https://b.de/y")
        assert a == b

    def test_the_reason_keeps_the_status_code(self):
        assert "429" in _reason("fantasyworld.be antwortet 429 (direkt) — Pause 60s")


class TestRender:
    def test_the_worst_shop_is_listed_first(self):
        text = render(
            rows(
                ("gut.de", None, 100),
                ("gut.de", "HTTP 429", 1),
                ("kaputt.de", "HTTP 503", 40),
                ("kaputt.de", None, 60),
            ),
            WINDOW,
        )
        assert text.index("kaputt.de") < text.index("gut.de")

    def test_the_rate_is_shown_not_just_the_count(self):
        """40 failures out of 50 checks and out of 5000 are different problems."""
        text = render(rows(("kaputt.de", "HTTP 503", 40), ("kaputt.de", None, 10)), WINDOW)
        assert "80%" in text

    def test_causes_are_grouped(self):
        text = render(
            rows(
                ("a.de", "brightdata returned an empty body for https://a.de/1", 3),
                ("a.de", "brightdata returned an empty body for https://a.de/2", 2),
            ),
            WINDOW,
        )
        assert "    5x  brightdata returned an empty body" in text

    def test_healthy_shops_are_counted_not_listed(self):
        text = render(rows(("gut.de", None, 10), ("auch-gut.de", None, 10)), WINDOW)
        assert "2 von 2" in text
        assert "gut.de " not in text.split("Ohne einen einzigen Fehler")[0].split("Shop")[-1]

    def test_an_empty_window_says_so(self):
        assert "Keine Prüfungen" in render([], WINDOW)


class TestDeadLinks:
    """Two shops ran at a 100 % failure rate: 2727 requests a day against pages
    that are gone. That is the biggest number in the report, it earns rate
    limits on shops we still need, and it is fixed by editing one watch."""

    def test_a_permanently_404ing_watch_is_named(self):
        found = dead_links(watch_rows("https://einzigundartig.de/p/weg", "HTTP 404", 700, "Alt"))

        assert found == [("Alt", "https://einzigundartig.de/p/weg", 700)]

    def test_an_occasional_404_is_not_a_dead_link(self):
        rows_ = watch_rows("https://shop.de/p/x", "HTTP 404", 2) + watch_rows(
            "https://shop.de/p/x", None, 98
        )
        assert dead_links(rows_) == []

    def test_other_failures_are_not_dead_links(self):
        """A rate-limited shop must not be reported as a wrong URL."""
        assert dead_links(watch_rows("https://shop.de/p/x", "RateLimited: 429", 500)) == []

    def test_the_report_names_them_with_the_wasted_count(self):
        text = render(
            watch_rows("https://einzigundartig.de/p/weg", "HTTP 404", 700, "Alte Watch"), WINDOW
        )
        assert "TOTE LINKS" in text
        assert "Alte Watch" in text
        assert "700" in text

    def test_a_clean_install_has_no_such_section(self):
        assert "TOTE LINKS" not in render(rows(("gut.de", None, 10)), WINDOW)
