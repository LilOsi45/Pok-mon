"""Health overview: tell apart a broken shop, an unreadable page and a dead job."""

from __future__ import annotations

from datetime import timedelta

from app.health_report import collect, render
from app.models import Game, ProductScan, StockStatus, Watch, utcnow


def watch(**kw) -> Watch:
    base = dict(
        id=1,
        game=Game.POKEMON,
        label="Testprodukt",
        url="https://shop.example/p",
        interval_seconds=120,
        enabled=True,
        last_check_at=utcnow(),
        last_status=StockStatus.IN_STOCK,
        last_error=None,
    )
    return Watch(**{**base, **kw})


class TestClassification:
    def test_recent_successful_check_is_ok(self):
        assert collect([watch()], [])[0].state == "OK"

    def test_recorded_error_is_a_failure(self):
        rows = collect([watch(last_error="all fetch attempts failed for https://x")], [])
        assert rows[0].state == "FEHLER"

    def test_unknown_status_is_its_own_category(self):
        """Fetched fine but stock unreadable — a parser problem, not a network one."""
        rows = collect([watch(last_status=StockStatus.UNKNOWN)], [])
        assert rows[0].state == "UNKLAR"

    def test_long_overdue_check_means_the_job_is_dead(self):
        rows = collect([watch(last_check_at=utcnow() - timedelta(minutes=30))], [])
        assert rows[0].state == "VERALTET"  # 30 min with a 120 s interval

    def test_never_checked_is_flagged(self):
        rows = collect([watch(last_check_at=None)], [])
        assert rows[0].state == "NIE GEPRÜFT"
        assert rows[0].age == "nie"

    def test_error_wins_over_a_stale_timestamp(self):
        """A stale check that also errored: the schedule problem is the real one."""
        rows = collect([watch(last_check_at=utcnow() - timedelta(hours=2), last_error="boom")], [])
        assert rows[0].state == "VERALTET"

    def test_scanners_are_included(self):
        scan = ProductScan(
            id=7,
            game=Game.POKEMON,
            label="Scanner",
            url="https://shop.example/c",
            keywords=["x"],
            interval_seconds=900,
            enabled=True,
            last_check_at=utcnow(),
            last_error="HTTP 429",
        )
        rows = collect([], [scan])
        assert rows[0].kind == "Scanner" and rows[0].state == "FEHLER"

    def test_problems_are_listed_before_healthy_rows(self):
        rows = collect([watch(id=1), watch(id=2, last_error="boom")], [])
        assert rows[0].id == 2


class TestRender:
    def test_all_healthy_says_so(self):
        out = render(collect([watch()], []))
        assert "1 von 1 in Ordnung" in out
        assert "Alles läuft" in out

    def test_groups_the_same_error(self):
        rows = collect(
            [
                watch(id=1, last_error="all fetch attempts failed for https://a"),
                watch(id=2, last_error="all fetch attempts failed for https://a"),
                watch(id=3),
            ],
            [],
        )
        out = render(rows)
        assert "1 von 3 in Ordnung" in out
        assert "2x FEHLER" in out
        assert "2x  all fetch attempts failed" in out

    def test_stale_rows_explain_the_restart(self):
        out = render(collect([watch(last_check_at=utcnow() - timedelta(hours=1))], []))
        assert "docker compose restart app" in out

    def test_healthy_rows_are_not_listed_individually(self):
        out = render(collect([watch(id=1, label="Gesund"), watch(id=2, last_error="x")], []))
        assert "Gesund" not in out
