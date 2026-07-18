"""Shop catalog + 'Auch checken' links in restock embeds."""

from __future__ import annotations

from app.events import Event
from app.models import EventType, Game
from app.notify.discord import build_embed
from app.shops import SHOP_CATALOG, other_shop_links, search_url, shops_for_game


class TestCatalog:
    def test_catalog_has_the_big_shops(self):
        domains = {s.domain for s in SHOP_CATALOG}
        for expected in ("card-corner.de", "games-island.eu", "fantasywelt.de", "amazon.de"):
            assert expected in domains

    def test_search_url_encodes_query(self):
        amazon = next(s for s in SHOP_CATALOG if s.domain == "amazon.de")
        assert search_url(amazon, "OP-12 Booster Display") == (
            "https://www.amazon.de/s?k=OP-12+Booster+Display"
        )

    def test_google_fallback_for_shops_without_template(self):
        mueller = next(s for s in SHOP_CATALOG if s.domain == "mueller.de")
        url = search_url(mueller, "KP11 Display")
        assert url.startswith("https://www.google.com/search?q=site%3Amueller.de+")

    def test_cardmarket_template_is_per_game(self):
        cm = next(s for s in SHOP_CATALOG if s.domain == "cardmarket.com")
        assert "/Pokemon/" in search_url(cm, "x", Game.POKEMON)
        assert "/OnePiece/" in search_url(cm, "x", Game.ONE_PIECE)

    def test_shops_for_game(self):
        assert shops_for_game(Game.POKEMON)
        assert all("pokemon" in s.games for s in shops_for_game("pokemon"))


class TestOtherShopLinks:
    def test_excludes_triggering_shop(self):
        links = other_shop_links("KP11", Game.POKEMON, exclude_url="https://www.amazon.de/dp/B0X")
        names = [n for n, _ in links]
        assert "Amazon.de" not in names
        assert len(links) > 3

    def test_limit(self):
        assert len(other_shop_links("x", limit=3)) == 3


class TestEmbedIntegration:
    def _event(self, type_: EventType) -> Event:
        return Event(
            type=type_,
            game=Game.ONE_PIECE,
            title="Back in stock: OP-09 Display",
            message="One Piece OP-09 Booster Display",
            url="https://www.games-island.eu/p/op09",
            routes=["stock:one_piece"],
        )

    def test_restock_embed_has_other_shops_field(self):
        embed = build_embed(self._event(EventType.BACK_IN_STOCK))
        field = next(f for f in embed["fields"] if f["name"] == "Auch checken")
        assert "Card Corner" in field["value"]
        assert "Games Island" not in field["value"]  # triggering shop excluded
        assert len(field["value"]) <= 1024

    def test_news_embed_has_no_other_shops_field(self):
        embed = build_embed(
            Event(type=EventType.NEW_SET_ANNOUNCED, game=Game.POKEMON, title="New set")
        )
        assert not any(f["name"] == "Auch checken" for f in embed["fields"])
