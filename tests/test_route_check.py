"""An alarm with no receiver must be named as such.

The Pokémon Center drop was missed twice over: the scanner watched the wrong
page, and the daily report that would have said so matched no notifier and was
dropped at INFO level. A watch can be perfectly healthy and still deliver
nothing. This report is the only place that checks the delivery side, so it has
to be right about "NIEMAND".
"""

from __future__ import annotations

from app.models import Game, ProductScan, Watch
from app.notify.discord import DiscordNotifier
from app.route_check import receivers, render

ALL_ROUTES = ["stock:*", "system:*", "news:*"]


def notifier(name: str, routes: list[str]) -> DiscordNotifier:
    return DiscordNotifier(name, {"webhook_url": "https://example.invalid/hook"}, routes)


def watch(label: str, channels: list[str], *, id: int = 1, enabled: bool = True) -> Watch:
    return Watch(
        id=id,
        game=Game.POKEMON,
        label=label,
        url="https://example.com/p",
        channels=channels,
        enabled=enabled,
    )


def scan(label: str, channels: list[str], *, id: int = 1) -> ProductScan:
    return ProductScan(
        id=id,
        game=Game.POKEMON,
        label=label,
        url="https://www.pokemoncenter.com/de-de/category/new-releases",
        channels=channels,
        enabled=True,
    )


class TestReceivers:
    def test_a_matching_pattern_is_listed(self):
        n = notifier("pc-alarm", ["channel:pc-queue"])
        assert receivers([n], ["stock:pokemon", "channel:pc-queue"]) == ["pc-alarm"]

    def test_no_match_is_empty(self):
        n = notifier("anderer", ["channel:etwas-anderes"])
        assert receivers([n], ["stock:pokemon", "channel:pc-queue"]) == []

    def test_the_probe_is_not_a_test_event(self):
        """`matches` returns True for EventType.TEST — probing with one would
        report every single gap as fine, which is the opposite of the job."""
        n = notifier("nur-news", ["news:*"])
        assert receivers([n], ["stock:pokemon", "channel:pc-queue"]) == []


class TestRender:
    def test_an_unrouted_watch_is_flagged(self):
        text = render(
            [notifier("nur-news", ["news:*"])],
            [watch("Pokémon Center Queue-Alarm DE", ["pc-queue"], id=63)],
            [],
        )
        assert "NIEMAND" in text
        assert "Pokémon Center Queue-Alarm DE" in text
        assert "channel:pc-queue" in text

    def test_a_fully_wired_setup_says_so(self):
        text = render(
            [notifier("pc-alarm", ["channel:pc-queue"]), notifier("rest", ALL_ROUTES)],
            [watch("Queue-Alarm", ["pc-queue"], id=63)],
            [scan("PC Neuheiten DE", ["pc-queue"], id=14)],
        )
        assert "Alles hat einen Empfänger." in text
        assert "NIEMAND" not in text

    def test_a_stock_only_notifier_leaves_the_heartbeat_orphaned(self):
        text = render([notifier("stock", ["stock:*"])], [watch("Ganz normal", [])], [])
        assert "Störungen bleiben unbemerkt" in text
        assert "heartbeat" in text

    def test_news_routes_are_checked_separately(self):
        """A stock+system notifier covers everything above and still no news."""
        text = render([notifier("alles-ausser-news", ["stock:*", "system:*"])], [], [])
        assert "Pokémon-News" in text
        assert "NIEMAND" in text

    def test_scanners_appear_with_their_own_ids(self):
        text = render(
            [notifier("nur-news", ["news:*"])], [], [scan("PC Neuheiten DE", ["pc-queue"], id=14)]
        )
        assert "SCANNER" in text
        assert "  14  PC Neuheiten DE" in text
        assert "Scanner 14 (PC Neuheiten DE)" in text

    def test_disabled_rows_are_skipped(self):
        text = render(
            [notifier("alles", ["*"])], [watch("Alte Doppel-Watch", [], enabled=False)], []
        )
        assert "Alte Doppel-Watch" not in text
        assert "(keine aktiven)" in text

    def test_no_notifier_at_all_says_so_plainly(self):
        assert "kein Notifier eingerichtet" in render([], [watch("Irgendeine", ["pc-queue"])], [])


class TestAvatarStatus:
    """The logo disappeared from every ping at once.

    Nothing in the sending code had changed, which leaves the URL — and the
    usual cause is specific: Discord's own attachment links are signed and
    expire, so a working avatar silently becomes a 404. The tool has to name
    that instead of leaving the operator to guess at a .env value.
    """

    def _status(self, monkeypatch, url: str | None):
        import app.config as config
        from app.route_check import avatar_status

        if url is None:
            monkeypatch.delenv("DISCORD_AVATAR_URL", raising=False)
        else:
            monkeypatch.setenv("DISCORD_AVATAR_URL", url)
        config.get_settings.cache_clear()
        try:
            return avatar_status()
        finally:
            config.get_settings.cache_clear()

    def test_an_expiring_discord_link_is_called_out(self, monkeypatch):
        text = self._status(
            monkeypatch,
            "https://cdn.discordapp.com/attachments/1/2/logo.png?ex=abc&is=def&hm=123",
        )
        assert "ACHTUNG" in text and "laufen nach kurzer Zeit ab" in text

    def test_plain_http_is_the_first_thing_named(self, monkeypatch):
        """The live setting was http://188.245.48.200:8000/static/img/… — Discord
        fetches the image itself and drops non-https without a word."""
        from app.route_check import avatar_problem

        problem = avatar_problem("http://188.245.48.200:8000/static/img/icon.png")
        assert problem and "https" in problem

    def test_a_bare_ip_is_named(self, monkeypatch):
        from app.route_check import avatar_problem

        problem = avatar_problem("https://188.245.48.200/static/img/icon.png")
        assert problem and "IP-Adresse" in problem

    def test_an_application_port_is_named(self, monkeypatch):
        from app.route_check import avatar_problem

        problem = avatar_problem("https://tracker.example.de:8000/static/img/icon.png")
        assert problem and "8000" in problem

    def test_a_permanent_url_is_just_reported(self, monkeypatch):
        text = self._status(monkeypatch, "https://holo.example/logo.png")
        assert "holo.example/logo.png" in text
        assert "ACHTUNG" not in text

    def test_no_url_explains_the_fallback(self, monkeypatch):
        text = self._status(monkeypatch, "")
        assert "keines eingestellt" in text
        assert "DISCORD_AVATAR_URL" in text
