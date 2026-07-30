"""Navigation links must not ping as drops.

Measured on pokemoncenter.com's German new-releases page: alongside 35 real
products the scanner matched "Alle Neuerscheinungen shoppen", "30 Jahre
Pokémon", "Alle Figuren & Pins shoppen" and "LEGO® Pokémon™" — every one a
/category/ link, every one long enough to pass the title-length heuristic. Those
would have arrived as restock alerts, and an alert channel that cries wolf stops
being read, which is the failure this whole tracker exists to avoid.

Blacklisting /category/ is not the fix: Shopify products live at
/collections/<x>/products/<y>. So a page that offers product-shaped links is
judged only on those.
"""

from __future__ import annotations

from app.scanner import parse_listing

PC = "https://www.pokemoncenter.com/de-de/category/new-releases"

PC_HTML = """
<html><body>
  <a href="/de-de/product/pokemon-tcg-top-trainer-box-mega">
     Pokémon-Sammelkartenspiel: Pokémon Center Top-Trainer-Box - Mega</a>
  <a href="/de-de/product/mini-tin-mega-lucario">
     Pokémon-Sammelkartenspiel: Mini-Tin-Box Mega-Helden: Mega-Lucario</a>
  <a href="/de-de/category/new-releases">Alle Neuerscheinungen shoppen</a>
  <a href="/de-de/category/pokemon-30">30 Jahre Pokémon</a>
  <a href="/de-de/category/figures-pins">Alle Figuren &amp; Pins shoppen</a>
  <a href="/de-de/category/lego">LEGO&#174; Pokémon&#8482;</a>
</body></html>
"""


class TestProductLinksWin:
    def test_only_products_are_returned(self):
        found = parse_listing(PC_HTML, PC)

        titles = sorted(p.title for p in found)
        assert len(found) == 2, f"navigation leaked in: {titles}"
        assert all("/product/" in p.url for p in found)

    def test_the_navigation_titles_are_gone(self):
        titles = " ".join(p.title for p in parse_listing(PC_HTML, PC))
        for nav in ("Alle Neuerscheinungen", "30 Jahre Pokémon", "LEGO"):
            assert nav not in titles

    def test_shopify_products_under_collections_still_count(self):
        """The obvious blacklist would have thrown these away."""
        html = """
        <html><body>
          <a href="/collections/pokemon/products/top-trainer-box-mega-helden">
             Pokémon Top-Trainer-Box Mega-Helden Sammlung</a>
          <a href="/collections/pokemon">Alle Pokémon Produkte ansehen</a>
        </body></html>
        """
        found = parse_listing(html, "https://shop.de/collections/pokemon")

        assert len(found) == 1
        assert "products/top-trainer-box" in found[0].url

    def test_a_shop_without_product_markers_still_works(self):
        """Narrowing must not empty a listing that has no such paths at all."""
        html = """
        <html><body>
          <a href="/shop/pokemon-top-trainer-box-mega-helden">
             Pokémon Top-Trainer-Box Mega-Helden</a>
          <a href="/shop/pokemon-elite-trainer-box-neu">
             Pokémon Elite Trainer Box Neuheit</a>
        </body></html>
        """
        found = parse_listing(html, "https://kleinshop.de/neu")

        assert len(found) == 2

    def test_legal_and_account_links_stay_excluded(self):
        html = """
        <html><body>
          <a href="/de-de/product/echte-box">Eine echte Produktbox mit langem Namen</a>
          <a href="/de-de/account/login">Mein Konto und Anmeldung hier</a>
        </body></html>
        """
        found = parse_listing(html, PC)
        assert [p.url.endswith("/echte-box") for p in found] == [True]

    def test_an_empty_page_yields_nothing(self):
        assert parse_listing("<html><body></body></html>", PC) == []


class TestEmptyKeywordsAreAllowed:
    """Forcing a keyword is what produced the filter "p".

    On a new-arrivals page every new product is the signal, so an empty filter
    is the correct setting. The form demanded one anyway, a single letter was
    entered to satisfy it, and every product without a "p" in its name was
    silently discarded — 12 of 47 on the page that mattered.
    """

    def test_no_keywords_matches_everything(self):
        from app.scanner import keywords_match

        assert keywords_match("Pokémon Top-Trainer-Box", []) is True
        assert keywords_match("Irgendwas ganz anderes", []) is True

    def test_the_form_does_not_demand_one(self):
        from pathlib import Path

        form = Path("app/web/templates/_scan_form.html").read_text(encoding="utf-8")
        keyword_line = next(line for line in form.splitlines() if 'name="keywords"' in line)
        assert "required" not in keyword_line

    def test_excludes_still_apply_without_keywords(self):
        from app.scanner import keywords_match

        assert keywords_match("Pokémon Sleeves", [], ["sleeves"]) is False
        assert keywords_match("Pokémon Display", [], ["sleeves"]) is True
