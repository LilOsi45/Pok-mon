"""Retailer adapter interface and shared result types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, Literal

from app.models import StockStatus


class AdapterError(Exception):
    """Recoverable adapter failure (bad page, blocked, not implemented)."""


class AdapterNotImplemented(AdapterError):
    """Raised by stub adapters that still need a parse implementation."""


class ProductNotFound(AdapterError):
    """The target page/product does not exist (yet) — relevant for NEW_LISTING."""


@dataclass(slots=True)
class PageResult:
    """Raw fetch result handed to `parse`."""

    url: str
    final_url: str
    status_code: int
    text: str = ""
    json_data: dict | list | None = None
    headers: dict = field(default_factory=dict)
    elapsed_ms: int = 0
    fetched_via: str = "httpx"


@dataclass(slots=True)
class StockResult:
    """Normalized parse result."""

    status: StockStatus
    price: float | None = None
    currency: str | None = "EUR"
    title: str | None = None
    image_url: str | None = None
    buy_url: str | None = None
    cart_url: str | None = None  # Shopify one-tap add-to-cart permalink
    # True when the product is listed on the retailer at all (even if OOS).
    # Drives NEW_LISTING; parse() should set False only when clearly absent.
    listed: bool = True
    note: str | None = None
    # Optional adapter-provided notification title (e.g. "Queue ist OFFEN")
    # overriding the generic "Back in stock: <label>" wording.
    alert_title: str | None = None
    # False when the page yielded no stock signal at all — no structured data,
    # no buy button, no sold-out phrase, no price. That is the absence of an
    # answer, not the answer "unknown", and it must not become the baseline a
    # restock is measured against: elbenwald.de occasionally returns a partial
    # page (40 KB instead of 533 KB), and recording that as UNKNOWN made the
    # next complete read look like UNKNOWN -> IN_STOCK, i.e. a restock ping for
    # a product whose availability had never changed.
    readable: bool = True


class RetailerAdapter(ABC):
    """One retailer integration.

    Subclasses set the class attributes and implement `parse`. `fetch` has a
    sensible default (httpx or Playwright, per `fetcher`) but may be overridden
    — e.g. API adapters that need signed requests.
    """

    slug: ClassVar[str] = "base"
    name: ClassVar[str] = "Base"
    domains: ClassVar[tuple[str, ...]] = ()
    fetcher: ClassVar[Literal["httpx", "playwright", "scraperapi"]] = "httpx"
    uses_api: ClassVar[bool] = False  # API adapters skip robots.txt (governed by API ToS)
    # Extra request headers per adapter (e.g. Accept-Language)
    extra_headers: ClassVar[dict[str, str]] = {}

    def __init__(self, detection_config: dict | None = None) -> None:
        self.detection_config = detection_config or {}

    async def fetch(self, url: str) -> PageResult:
        from app.monitor import fetchers

        if self.fetcher == "scraperapi":
            return await fetchers.fetch_scraperapi(url, extra_headers=self.extra_headers)
        if self.fetcher == "playwright":
            return await fetchers.fetch_playwright(url, extra_headers=self.extra_headers)
        return await fetchers.fetch_httpx(
            url, extra_headers=self.extra_headers, respect_robots=not self.uses_api
        )

    @abstractmethod
    def parse(self, page: PageResult) -> StockResult:
        """Turn a PageResult into a StockResult. Raise AdapterError on failure."""

    async def check(self, url: str) -> tuple[PageResult, StockResult]:
        page = await self.fetch(url)
        if page.status_code == 404:
            return page, StockResult(status=StockStatus.UNKNOWN, listed=False, note="HTTP 404")
        return page, self.parse(page)
