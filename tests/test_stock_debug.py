"""Fixing a shop's stock reading meant guessing at its HTML from outside.

elbenwald.de pinged a product that could not be ordered, and the only way to
tell what its buy button looks like was to ask someone to paste the page. The
unrecognised-button list turns that into one command.
"""

from __future__ import annotations

from app.stock_debug import report

SHOPWARE = """
<html><body>
  <div itemscope itemtype="https://schema.org/Product">
    <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
      <link itemprop="availability" href="https://schema.org/InStock">
    </div>
  </div>
  <button class="btn-buy" data-x="1">Jetzt bestellen</button>
  <button disabled>Benachrichtigen</button>
  <a role="button" href="/merkzettel">Auf den Merkzettel</a>
</body></html>
"""


class TestReport:
    def test_it_lists_the_buttons_it_did_not_recognise(self):
        text = report("https://www.elbenwald.de/x", SHOPWARE)

        assert "Jetzt bestellen" in text
        assert "nicht erkannt" in text

    def test_a_greyed_out_button_is_marked_as_such(self):
        text = report("https://www.elbenwald.de/x", SHOPWARE)
        assert "ausgegraut" in text

    def test_a_recognised_button_says_so(self):
        html = SHOPWARE.replace("Jetzt bestellen", "In den Warenkorb")
        assert "ERKANNT" in report("https://www.elbenwald.de/x", html)

    def test_the_signals_and_the_verdict_are_shown(self):
        text = report("https://www.elbenwald.de/x", SHOPWARE)

        assert "Microdata" in text
        assert "URTEIL" in text

    def test_a_javascript_page_says_there_are_no_buttons(self):
        text = report("https://shop.de/x", "<html><body><div id='app'></div></body></html>")
        assert "keine gefunden" in text
