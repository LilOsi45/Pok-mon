"""MediaMarkt/Saturn in-store (market pickup) adapter detection."""

from __future__ import annotations

from app.models import StockStatus
from app.monitor.adapters.mediamarkt_instore import (
    MediaMarktInStoreAdapter,
    SaturnInStoreAdapter,
)
from app.monitor.base import PageResult
from app.monitor.registry import resolve_adapter


def page(
    text: str, status: int = 200, url="https://www.mediamarkt.de/de/product/x.html"
) -> PageResult:
    return PageResult(
        url=url, final_url=url, status_code=status, text=text, fetched_via="playwright"
    )


class TestDetection:
    def test_available_in_stores(self):
        html = "<html><body><h1>KP11 Display</h1><div>Abholung: in 12 Märkten verfügbar</div></body></html>"
        result = MediaMarktInStoreAdapter().parse(page(html))
        assert result.status == StockStatus.IN_STOCK
        assert result.alert_title and "Markt" in result.alert_title

    def test_not_available_in_stores(self):
        html = "<html><body><h1>KP11 Display</h1><div>In keinem Markt verfügbar</div></body></html>"
        result = MediaMarktInStoreAdapter().parse(page(html))
        assert result.status == StockStatus.OUT_OF_STOCK

    def test_bot_wall_is_unknown(self):
        html = "<html><head><title>Just a moment...</title></head><body>cf-challenge</body></html>"
        result = MediaMarktInStoreAdapter().parse(page(html))
        assert result.status == StockStatus.UNKNOWN
        assert "bot wall" in result.note

    def test_no_signal_is_unknown(self):
        html = "<html><body><h1>Ein Produkt</h1><p>Nur Online-Text</p></body></html>"
        assert MediaMarktInStoreAdapter().parse(page(html)).status == StockStatus.UNKNOWN


class TestRegistry:
    def test_uses_scraperapi(self):
        assert MediaMarktInStoreAdapter().fetcher == "scraperapi"

    def test_not_domain_resolved(self):
        # a normal mediamarkt URL keeps the online-stock adapter
        assert (
            resolve_adapter("https://www.mediamarkt.de/de/product/x.html").slug == "mediamarkt_de"
        )

    def test_selectable_by_slug(self):
        assert (
            resolve_adapter(
                "https://www.mediamarkt.de/de/product/x.html", slug="mediamarkt_instore"
            ).slug
            == "mediamarkt_instore"
        )
        assert (
            resolve_adapter("https://www.saturn.de/de/product/x.html", slug="saturn_instore").slug
            == "saturn_instore"
        )


def test_saturn_subclass():
    assert SaturnInStoreAdapter().fetcher == "scraperapi"
    assert SaturnInStoreAdapter.slug == "saturn_instore"
