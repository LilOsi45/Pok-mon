"""Adapter parse() tests against saved fixture pages/JSON."""

from __future__ import annotations

import json

from app.models import StockStatus
from app.monitor.adapters.amazon_de import AmazonDeAdapter
from app.monitor.adapters.cardmarket import CardmarketAdapter
from app.monitor.adapters.games_island import GamesIslandAdapter
from app.monitor.adapters.generic import GenericAdapter
from app.monitor.adapters.mediamarkt_de import MediaMarktDeAdapter
from app.monitor.base import PageResult
from app.monitor.registry import resolve_adapter
from tests.conftest import load_fixture


def page(
    text: str = "", status: int = 200, json_data=None, url="https://example.com/p"
) -> PageResult:
    return PageResult(url=url, final_url=url, status_code=status, text=text, json_data=json_data)


class TestAmazonDe:
    def test_in_stock(self):
        result = AmazonDeAdapter().parse(page(load_fixture("amazon_instock.html")))
        assert result.status == StockStatus.IN_STOCK
        assert result.price == 149.99
        assert "KP11" in result.title
        assert result.image_url.endswith("kp11-display.jpg")

    def test_out_of_stock(self):
        result = AmazonDeAdapter().parse(page(load_fixture("amazon_oos.html")))
        assert result.status == StockStatus.OUT_OF_STOCK

    def test_captcha_is_unknown_not_oos(self):
        result = AmazonDeAdapter().parse(page(load_fixture("amazon_captcha.html")))
        assert result.status == StockStatus.UNKNOWN
        assert "captcha" in result.note


class TestMediaMarkt:
    def test_jsonld_in_stock(self):
        result = MediaMarktDeAdapter().parse(page(load_fixture("jsonld_instock.html")))
        assert result.status == StockStatus.IN_STOCK
        assert result.price == 54.99

    def test_cloudflare_challenge(self):
        html = "<html><head><title>Just a moment...</title></head><body>cf-challenge</body></html>"
        result = MediaMarktDeAdapter().parse(page(html))
        assert result.status == StockStatus.UNKNOWN


class TestCardmarket:
    def test_in_stock_with_price_guide(self):
        data = json.loads(load_fixture("cardmarket_product.json"))
        result = CardmarketAdapter().parse(page(json_data=data))
        assert result.status == StockStatus.IN_STOCK
        assert result.price == 119.9  # LOW preferred
        assert result.title == "Scarlet & Violet Booster Box"
        assert result.image_url.startswith("https://")
        assert result.buy_url.startswith("https://www.cardmarket.com/")

    def test_out_of_stock(self):
        data = json.loads(load_fixture("cardmarket_oos.json"))
        result = CardmarketAdapter().parse(page(json_data=data))
        assert result.status == StockStatus.OUT_OF_STOCK
        assert result.price == 289.9  # falls back to TREND

    def test_min_articles_threshold(self):
        data = json.loads(load_fixture("cardmarket_product.json"))
        result = CardmarketAdapter({"min_articles": 100}).parse(page(json_data=data))
        assert result.status == StockStatus.OUT_OF_STOCK

    def test_product_id_from_url(self):
        adapter = CardmarketAdapter()
        assert (
            adapter.product_id("https://api.cardmarket.com/ws/v2.0/output.json/products/123456")
            == 123456
        )

    def test_product_id_from_config(self):
        assert (
            CardmarketAdapter({"id_product": 777}).product_id("https://www.cardmarket.com/x") == 777
        )


class TestGamesIsland:
    def test_in_stock_badge(self):
        result = GamesIslandAdapter().parse(page(load_fixture("gamesisland_instock.html")))
        assert result.status == StockStatus.IN_STOCK
        assert result.price == 109.9
        assert "OP-09" in result.title

    def test_oos_badge(self):
        result = GamesIslandAdapter().parse(page(load_fixture("gamesisland_oos.html")))
        assert result.status == StockStatus.OUT_OF_STOCK


class TestRegistry:
    def test_resolve_by_domain(self):
        assert resolve_adapter("https://www.amazon.de/dp/B0ABC").slug == "amazon_de"
        assert (
            resolve_adapter("https://www.mediamarkt.de/de/product/x.html").slug == "mediamarkt_de"
        )
        assert resolve_adapter("https://www.games-island.eu/p/x").slug == "games_island"
        assert (
            resolve_adapter("https://api.cardmarket.com/ws/v2.0/output.json/products/1").slug
            == "cardmarket"
        )

    def test_unknown_domain_falls_back_to_generic(self):
        assert isinstance(resolve_adapter("https://shop.unknown-store.de/p/1"), GenericAdapter)

    def test_explicit_slug_wins(self):
        assert (
            resolve_adapter("https://whatever.example/x", slug="games_island").slug
            == "games_island"
        )

    def test_fetcher_override(self):
        adapter = resolve_adapter(
            "https://shop.example.de/p", detection_config={"fetcher": "playwright"}
        )
        assert adapter.fetcher == "playwright"
