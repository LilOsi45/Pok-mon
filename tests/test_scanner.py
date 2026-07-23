"""Keyword scanner: listing parsing, matching, baseline and new-product events."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import EventType, Game, ProductScan, ScanItem
from app.monitor.base import PageResult
from app.scanner import (
    FLOOD_CAP,
    keywords_match,
    parse_listing,
    run_scan,
    url_key,
)
from tests.conftest import load_fixture

BASE = "https://www.beispiel-shop.de/pokemon/neuheiten"


def _page(text: str, status: int = 200) -> PageResult:
    return PageResult(url=BASE, final_url=BASE, status_code=status, text=text)


class TestParsing:
    def test_extracts_products_and_skips_nav(self):
        products = parse_listing(load_fixture("listing_page.html"), BASE)
        titles = [p.title for p in products]
        assert any("KP12 Booster Display" in t for t in titles)
        assert any("Top-Trainer-Box" in t for t in titles)
        assert any("OP-13" in t for t in titles)
        # nav/service links and offsite products are not extracted
        joined = " ".join(p.url for p in products)
        assert "/login" not in joined
        assert "/impressum" not in joined
        assert "other-shop.example.com" not in joined

    def test_jsonld_price_merged_with_anchor(self):
        products = parse_listing(load_fixture("listing_page.html"), BASE)
        display = next(p for p in products if "KP12 Booster Display" in p.title)
        assert display.price == 159.99

    def test_url_key_normalizes(self):
        assert url_key("https://www.shop.de/p/x/?utm=1#top") == url_key("https://www.shop.de/p/x")


class TestMatching:
    def test_accent_insensitive(self):
        assert keywords_match("Pokémon KP12 Booster Display", ["pokemon", "display"])

    def test_all_keywords_required(self):
        assert not keywords_match("Pokémon KP12 Top-Trainer-Box", ["pokemon", "display"])

    def test_exclude_wins(self):
        assert not keywords_match("Pokémon Sleeves 100 Stück", ["pokemon"], exclude=["sleeves"])


def make_scan(**overrides) -> ProductScan:
    defaults = dict(
        game=Game.POKEMON,
        label="Beispiel-Shop Pokémon",
        url=BASE,
        keywords=["pokemon", "display"],
        exclude_keywords=[],
        interval_seconds=900,
        channels=["pokemon"],
        enabled=True,
    )
    defaults.update(overrides)
    return ProductScan(**defaults)


@pytest.mark.asyncio
async def test_baseline_then_new_product_fires_once(session):
    scan = make_scan()
    session.add(scan)
    await session.commit()

    html = load_fixture("listing_page.html")
    with (
        patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=_page(html))),
        patch("app.scanner.dispatch_event", AsyncMock()) as dispatched,
    ):
        # 1st run: baseline — the KP12 display is recorded silently
        created = await run_scan(session, scan)
        assert len(created) == 1  # only the display matches "pokemon"+"display"
        assert dispatched.call_count == 0
        assert scan.baseline_done is True

        # 2nd run, same page: nothing new
        assert await run_scan(session, scan) == []
        assert dispatched.call_count == 0

    # 3rd run: a new matching product appears on the page
    html_new = html.replace(
        "</main>",
        '<div class="product-card"><a href="/pokemon/kp13-neues-set-display">'
        "Pokémon KP13 Neues Set Booster Display</a><span>164,99 €</span></div></main>",
    )
    with (
        patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=_page(html_new))),
        patch("app.scanner.dispatch_event", AsyncMock()) as dispatched,
    ):
        created = await run_scan(session, scan)
        assert len(created) == 1
        assert dispatched.call_count == 1
        event = dispatched.call_args.args[1]
        assert event.type == EventType.NEW_LISTING
        assert "KP13" in event.title
        assert "stock:pokemon" in event.routes

        # 4th run: the product is known now, no repeat ping
        assert await run_scan(session, scan) == []
        assert dispatched.call_count == 1


@pytest.mark.asyncio
async def test_price_change_pings(session):
    scan = make_scan()
    session.add(scan)
    await session.commit()

    html = load_fixture("listing_page.html")
    with (
        patch("app.monitor.fetchers.fetch_httpx", AsyncMock(return_value=_page(html))),
        patch("app.scanner.dispatch_event", AsyncMock()) as dispatched,
    ):
        await run_scan(session, scan)  # baseline records the display @ 159.99
        assert dispatched.call_count == 0

    # same product, cheaper now → one PRICE_DROP ping
    with (
        patch(
            "app.monitor.fetchers.fetch_httpx",
            AsyncMock(return_value=_page(html.replace("159,99", "129,99"))),
        ),
        patch("app.scanner.dispatch_event", AsyncMock()) as dispatched,
    ):
        assert await run_scan(session, scan) == []  # nothing NEW
        assert dispatched.call_count == 1
        event = dispatched.call_args.args[1]
        assert event.type == EventType.PRICE_DROP
        assert event.price == 129.99
        assert "Preis" in event.title


@pytest.mark.asyncio
async def test_flood_sends_single_summary(session):
    scan = make_scan(keywords=["pokemon"])
    session.add(scan)
    await session.commit()

    with (
        patch(
            "app.monitor.fetchers.fetch_httpx",
            AsyncMock(return_value=_page("<html><body></body></html>")),
        ),
        patch("app.scanner.dispatch_event", AsyncMock()),
    ):
        await run_scan(session, scan)  # empty baseline

    cards = "".join(
        f'<div><a href="/pokemon/produkt-{i}">Pokémon Produkt Nummer {i} Booster</a></div>'
        for i in range(FLOOD_CAP + 4)
    )
    with (
        patch(
            "app.monitor.fetchers.fetch_httpx",
            AsyncMock(return_value=_page(f"<html><body>{cards}</body></html>")),
        ),
        patch("app.scanner.dispatch_event", AsyncMock()) as dispatched,
    ):
        created = await run_scan(session, scan)
    assert len(created) == FLOOD_CAP + 4
    assert dispatched.call_count == 1  # one summary, not a dozen pings
    assert f"{FLOOD_CAP + 4} neue Produkte" in dispatched.call_args.args[1].title


@pytest.mark.asyncio
async def test_fetch_error_recorded_no_baseline(session):
    scan = make_scan()
    session.add(scan)
    await session.commit()
    with patch(
        "app.monitor.fetchers.fetch_httpx",
        AsyncMock(return_value=_page("oops", status=503)),
    ):
        created = await run_scan(session, scan)
    assert created == []
    assert scan.baseline_done is False  # errors must not swallow the baseline
    assert "503" in scan.last_error
    items = (await session.execute(select(ScanItem))).scalars().all()
    assert items == []
