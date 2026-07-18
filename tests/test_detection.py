"""Detection strategy tests: JSON-LD, price parsing, sold-out/buy-button heuristics."""

from __future__ import annotations

import pytest

from app.models import StockStatus
from app.monitor.detection import (
    detect_stock,
    extract_price_from_text,
    parse_german_price,
    parse_json_ld,
)
from tests.conftest import load_fixture


class TestPriceParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("54,99", 54.99),
            ("1.234,56", 1234.56),
            ("1,234.56", 1234.56),
            ("149,99 €", 149.99),
            ("€ 49.99", 49.99),
            ("EUR 12,50", 12.5),
            (119.99, 119.99),
            (120, 120.0),
            ("", None),
            ("kein preis", None),
        ],
    )
    def test_parse_german_price(self, raw, expected):
        assert parse_german_price(raw) == expected

    def test_extract_from_text(self):
        assert extract_price_from_text("Jetzt für nur 109,90 € kaufen") == 109.90
        assert extract_price_from_text("no price here") is None


class TestJsonLd:
    def test_instock_graph_with_german_price(self):
        result = parse_json_ld(load_fixture("jsonld_instock.html"))
        assert result is not None
        assert result.status == StockStatus.IN_STOCK
        assert result.price == 54.99
        assert result.title == "Pokémon KP11 Top-Trainer-Box"
        assert result.image_url == "https://cdn.example.com/kp11-ttb.jpg"
        assert result.buy_url == "https://shop.example.com/kp11-ttb"

    def test_oos_offer_list(self):
        result = parse_json_ld(load_fixture("jsonld_oos.html"))
        assert result is not None
        assert result.status == StockStatus.OUT_OF_STOCK
        assert result.price == 119.99

    def test_no_jsonld(self):
        assert parse_json_ld("<html><body>plain page</body></html>") is None


class TestHeuristics:
    def test_buy_button_beats_nothing(self):
        html = """<html><body><h1>Produkt</h1><span>49,99 €</span>
        <button class="btn">In den Warenkorb</button></body></html>"""
        result = detect_stock(html)
        assert result.status == StockStatus.IN_STOCK
        assert result.price == 49.99

    def test_sold_out_phrase_wins_over_button(self):
        html = """<html><body><h1>Produkt</h1><p>Dieser Artikel ist ausverkauft.</p>
        <button disabled>In den Warenkorb</button></body></html>"""
        result = detect_stock(html)
        assert result.status == StockStatus.OUT_OF_STOCK

    def test_english_sold_out(self):
        html = "<html><body><p>This item is currently sold out.</p></body></html>"
        assert detect_stock(html).status == StockStatus.OUT_OF_STOCK

    def test_no_signal_is_unknown(self):
        html = "<html><body><p>Welcome to our shop.</p></body></html>"
        assert detect_stock(html).status == StockStatus.UNKNOWN

    def test_jsonld_preferred_over_text(self):
        # JSON-LD says InStock even though the page shows a sold-out banner elsewhere
        html = load_fixture("jsonld_instock.html").replace("</body>", "<p>ausverkauft</p></body>")
        assert detect_stock(html).status == StockStatus.IN_STOCK
