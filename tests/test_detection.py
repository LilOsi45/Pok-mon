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


class TestMicrodataAvailability:
    """elbenwald.de: 509 KB Seite, Preis 69,95 EUR gelesen — Urteil "no signal".

    The shop marks availability up as HTML attributes (Shopware microdata)
    instead of a JSON-LD Offer. Without reading those, the watch could never
    report stock either way, which is an alarm that can never fire.
    """

    SHOPWARE = """
    <html lang="de-DE" itemscope itemtype="https://schema.org/WebPage"><body>
      <div itemscope itemtype="https://schema.org/Product">
        <meta itemprop="name" content="Pokémon 30 Jahre Top-Trainer-Box">
        <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
          <meta itemprop="price" content="69.95">
          <link itemprop="availability" href="https://schema.org/InStock">
        </div>
      </div>
    </body></html>
    """

    def test_in_stock_microdata_is_read(self):
        from app.monitor.detection import detect_stock

        result = detect_stock(self.SHOPWARE)
        assert result.status is StockStatus.IN_STOCK
        assert result.note == "microdata"

    def test_out_of_stock_microdata_is_read(self):
        from app.monitor.detection import detect_stock

        html = self.SHOPWARE.replace("InStock", "OutOfStock")
        assert detect_stock(html).status is StockStatus.OUT_OF_STOCK

    def test_a_sold_out_phrase_beats_stale_markup(self):
        """Shops forget to update the itemprop far more often than they leave
        the words "ausverkauft" on a buyable article."""
        from app.monitor.detection import detect_stock

        html = self.SHOPWARE.replace("</body>", "<p>Leider ausverkauft</p></body>")
        assert detect_stock(html).status is StockStatus.OUT_OF_STOCK

    def test_the_page_type_itemtype_is_not_mistaken_for_availability(self):
        from selectolax.parser import HTMLParser

        from app.monitor.detection import parse_microdata

        html = '<html itemscope itemtype="https://schema.org/WebPage"><body>x</body></html>'
        assert parse_microdata(HTMLParser(html)) is None

    def test_no_markup_still_yields_no_signal(self):
        from app.monitor.detection import detect_stock

        assert detect_stock("<html><body>Irgendwas</body></html>").status is StockStatus.UNKNOWN
