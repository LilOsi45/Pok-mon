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


class TestFetchFacts:
    """40 KB with no buttons looked like "JavaScript" — but the same shop had
    already served 509 KB with a readable price on another page. Status code,
    landing URL and route are what tell a 404 page, a consent wall and a real
    JS shell apart, and the report left all three out."""

    def _page(self, **kw):
        from app.monitor.base import PageResult

        base = dict(
            url="https://www.elbenwald.de/x",
            final_url="https://www.elbenwald.de/x",
            status_code=200,
            text="<html></html>",
            fetched_via="browser-tls",
        )
        return PageResult(**{**base, **kw})

    def test_status_and_route_are_shown(self):
        from app.stock_debug import _fetch_line

        lines = "\n".join(_fetch_line(self._page()))
        assert "200" in lines and "browser-tls" in lines

    def test_a_redirect_is_named(self):
        from app.stock_debug import _fetch_line

        lines = "\n".join(_fetch_line(self._page(final_url="https://www.elbenwald.de/404")))
        assert "Gelandet auf" in lines and "/404" in lines

    def test_no_redirect_stays_quiet(self):
        from app.stock_debug import _fetch_line

        assert "Gelandet auf" not in "\n".join(_fetch_line(self._page()))
