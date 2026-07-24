"""The browser fetcher must let JS-rendered listings finish loading.

Regression: with only a fixed 2.5s sleep a slow shop was snapshotted before
its product grid arrived, so the scanner reported "0 products" on one run and
hundreds on the next.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.monitor import fetchers


class FakePage:
    def __init__(self, *, idle_raises: bool = False) -> None:
        self.calls: list[str] = []
        self._idle_raises = idle_raises
        self.url = "https://shop.example/list"

    async def route(self, *_args, **_kwargs) -> None:
        pass

    async def goto(self, *_args, **_kwargs):
        self.calls.append("goto")
        return MagicMock(status=200)

    async def wait_for_load_state(self, state: str, **_kwargs) -> None:
        self.calls.append(f"idle:{state}")
        if self._idle_raises:
            raise TimeoutError("never settled")

    async def wait_for_timeout(self, _ms: int) -> None:
        self.calls.append("sleep")

    async def content(self) -> str:
        self.calls.append("content")
        return "<html>products</html>"


def _browser_with(page: FakePage) -> MagicMock:
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    return browser


async def _run(page: FakePage) -> fetchers.PageResult:
    with (
        patch.object(fetchers, "_get_browser", AsyncMock(return_value=_browser_with(page))),
        patch.object(fetchers.asyncio, "sleep", AsyncMock()),
    ):
        return await fetchers.fetch_playwright("https://shop.example/list")


@pytest.mark.asyncio
async def test_waits_for_network_idle_before_snapshotting():
    page = FakePage()
    result = await _run(page)
    assert result.status_code == 200
    # the snapshot must come after the idle wait, not straight after goto
    assert page.calls.index("idle:networkidle") < page.calls.index("content")


@pytest.mark.asyncio
async def test_page_that_never_goes_idle_still_returns_html():
    # ad/polling pages never reach networkidle — the fixed wait must still apply
    page = FakePage(idle_raises=True)
    result = await _run(page)
    assert result.text == "<html>products</html>"
    assert page.calls.index("sleep") < page.calls.index("content")
