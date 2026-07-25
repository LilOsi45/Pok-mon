"""Row action buttons are wired through data-* + rows.js, not hx-* per button.

Guards the performance fix: hx-* on every row button meant htmx re-initialised
hundreds of elements on every table refresh. If someone reintroduces hx-* here,
the dashboard silently gets slow again on a phone — so assert the shape.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth import require_auth
from app.db import get_session
from app.main import app
from app.models import Game, ProductScan, Watch

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"
ROW_TEMPLATES = ["_watch_rows.html", "_scan_rows.html"]


@pytest_asyncio.fixture
async def client(session):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_auth] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


class TestTemplatesStayDelegated:
    @pytest.mark.parametrize("name", ROW_TEMPLATES)
    def test_no_hx_attributes_on_row_buttons(self, name: str):
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "hx-post" not in html, f"{name}: hx-post is back — that kills the perf fix"
        assert "hx-get" not in html
        assert "hx-confirm" not in html

    @pytest.mark.parametrize("name", ROW_TEMPLATES)
    def test_every_action_button_declares_url_and_target(self, name: str):
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        buttons = re.findall(r"<button\b[^>]*>", html)
        assert buttons, f"{name}: no buttons found — template changed shape?"
        for button in buttons:
            assert "data-url=" in button, f"{name}: button without data-url: {button}"
            assert "data-target=" in button, f"{name}: button without data-target: {button}"

    def test_read_only_actions_ask_for_get(self):
        html = (TEMPLATES / "_watch_rows.html").read_text(encoding="utf-8")
        for button in re.findall(r"<button\b[^>]*>", html):
            reads = "/edit" in button or "/history" in button
            assert reads == ('data-method="GET"' in button), f"wrong method on {button}"

    def test_delete_still_confirms(self):
        for name in ROW_TEMPLATES:
            html = (TEMPLATES / name).read_text(encoding="utf-8")
            delete = next(b for b in re.findall(r"<button\b[^>]*>", html) if "/delete" in b)
            assert "data-confirm=" in delete


class TestRenderedPage:
    @pytest.mark.asyncio
    async def test_watch_rows_render_the_expected_wiring(self, client, session):
        watch = Watch(
            game=Game.POKEMON,
            label="Testprodukt",
            url="https://shop.example/p",
            channels=["tcg-check"],
        )
        session.add(watch)
        await session.commit()

        html = (await client.get("/watches")).text
        assert f'data-url="/watches/{watch.id}/check"' in html
        assert f'data-url="/watches/{watch.id}/edit" data-method="GET"' in html
        assert 'data-target="#watch-rows"' in html

    @pytest.mark.asyncio
    async def test_htmx_element_count_does_not_grow_with_rows(self, client, session):
        """The whole point of the fix: htmx work must be independent of list size.

        Before, every row added five hx-* elements, so a table refresh with 60
        watches re-initialised ~300 of them.
        """

        def hx_count(html: str) -> int:
            return len(re.findall(r"hx-(get|post)=", html))

        def make(n: int) -> list[Watch]:
            return [
                Watch(
                    game=Game.POKEMON,
                    label=f"Produkt {i}",
                    url=f"https://shop.example/{i}",
                    channels=["tcg-check"],
                )
                for i in range(n)
            ]

        session.add_all(make(2))
        await session.commit()
        few = hx_count((await client.get("/watches")).text)

        session.add_all(make(20))
        await session.commit()
        many = hx_count((await client.get("/watches")).text)

        assert few == many, f"htmx elements grew from {few} to {many} with more rows"
        # the polled tbody itself carries none at all
        assert hx_count((await client.get("/watches/table")).text) == 0

    @pytest.mark.asyncio
    async def test_scan_rows_render_the_expected_wiring(self, client, session):
        scan = ProductScan(
            game=Game.POKEMON,
            label="Testscanner",
            url="https://shop.example/c",
            keywords=["pokemon"],
            channels=["tcg-check"],
        )
        session.add(scan)
        await session.commit()

        html = (await client.get("/scanner")).text
        assert f'data-url="/scanner/{scan.id}/check"' in html
        assert 'data-target="#scan-rows"' in html

    @pytest.mark.asyncio
    async def test_polling_only_runs_while_the_tab_is_visible(self, client):
        """A background tab must not keep rebuilding the table."""
        html = (await client.get("/watches")).text
        assert "document.visibilityState === 'visible'" in html

    @pytest.mark.asyncio
    async def test_no_third_party_stylesheet(self, client):
        """A render-blocking font from Google left the page blank on mobile."""
        html = (await client.get("/watches")).text
        assert "fonts.googleapis.com" not in html
        assert "fonts.gstatic.com" not in html
        assert "/static/css/fonts.css" in html
