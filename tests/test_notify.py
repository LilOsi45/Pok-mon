"""Routing matcher + Discord embed building (no network)."""

from __future__ import annotations

from app.events import Event, route_matches, stock_routes
from app.models import EventType, Game
from app.notify.discord import COLOR_MAGENTA, COLOR_VIOLET, DiscordNotifier, build_embed


class TestRouteMatching:
    def test_wildcard(self):
        assert route_matches("*", ["stock:pokemon"])

    def test_exact(self):
        assert route_matches("stock:pokemon", ["stock:pokemon", "watch:3"])
        assert not route_matches("stock:one_piece", ["stock:pokemon"])

    def test_prefix_wildcard(self):
        assert route_matches("news:*", ["news:one_piece"])
        assert not route_matches("news:*", ["stock:pokemon"])

    def test_bare_word(self):
        assert route_matches("pokemon", ["stock:pokemon"])
        assert not route_matches("pokemon", ["stock:one_piece"])

    def test_channel_routes(self):
        routes = stock_routes(Game.POKEMON, ["hot-drops"])
        assert route_matches("channel:hot-drops", routes)


class TestNotifierMatching:
    def _event(self, routes: list[str], type_: EventType = EventType.BACK_IN_STOCK) -> Event:
        return Event(type=type_, game=Game.POKEMON, title="x", routes=routes)

    def test_matching_notifier(self):
        n = DiscordNotifier("poke", {}, ["stock:pokemon"])
        assert n.matches(self._event(["stock:pokemon"]))
        assert not n.matches(self._event(["stock:one_piece"]))

    def test_test_event_matches_everything(self):
        n = DiscordNotifier("op-only", {}, ["stock:one_piece"])
        assert n.matches(self._event(["anything"], type_=EventType.TEST))


class TestDiscordEmbed:
    def test_restock_embed_is_magenta_with_fields(self):
        event = Event(
            type=EventType.BACK_IN_STOCK,
            game=Game.ONE_PIECE,
            title="Back in stock: OP-09 Display",
            url="https://shop.example/op09",
            image_url="https://cdn.example/op09.jpg",
            price=109.9,
            currency="EUR",
            retailer="Games Island",
            routes=["stock:one_piece"],
        )
        embed = build_embed(event)
        assert embed["color"] == COLOR_MAGENTA
        assert embed["url"] == "https://shop.example/op09"
        assert embed["thumbnail"]["url"] == "https://cdn.example/op09.jpg"
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["Game"] == "One Piece Card Game"
        assert fields["Retailer"] == "Games Island"
        assert fields["Price"] == "109.90 €"
        assert "Buy" in fields

    def test_news_embed_is_violet(self):
        event = Event(
            type=EventType.NEW_SET_ANNOUNCED,
            game=Game.POKEMON,
            title="Mega Evolution revealed",
            routes=["news:pokemon"],
        )
        assert build_embed(event)["color"] == COLOR_VIOLET
