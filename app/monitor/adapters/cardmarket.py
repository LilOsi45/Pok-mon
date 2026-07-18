"""Cardmarket adapter — uses the official Cardmarket API (never scrapes).

Requires a (free) Cardmarket API app: https://api.cardmarket.com
Set in .env: CARDMARKET_APP_TOKEN, CARDMARKET_APP_SECRET,
CARDMARKET_ACCESS_TOKEN, CARDMARKET_ACCESS_SECRET.

Watch configuration — the watch needs a Cardmarket product id, either:
  - detection config: {"id_product": 123456}, or
  - watch URL of the form https://api.cardmarket.com/ws/v2.0/output.json/products/123456

"In stock" means the product has sellable articles (countArticles > 0,
optionally filtered to countFoils etc. later). Price comes from the price
guide (LOW, falling back to TREND/AVG).
"""

from __future__ import annotations

import re

import httpx

from app.config import get_settings
from app.models import StockStatus
from app.monitor.base import AdapterError, PageResult, RetailerAdapter, StockResult
from app.monitor.oauth1 import build_oauth1_header

API_BASE = "https://api.cardmarket.com/ws/v2.0/output.json"
_PRODUCT_ID_RE = re.compile(r"/products/(\d+)")


class CardmarketAdapter(RetailerAdapter):
    slug = "cardmarket"
    name = "Cardmarket"
    domains = ("cardmarket.com", "www.cardmarket.com", "api.cardmarket.com")
    uses_api = True

    def product_id(self, url: str) -> int:
        if id_product := self.detection_config.get("id_product"):
            return int(id_product)
        if m := _PRODUCT_ID_RE.search(url):
            return int(m.group(1))
        raise AdapterError(
            "cardmarket watch needs detection config {'id_product': <id>} or an API product URL "
            "(https://api.cardmarket.com/ws/v2.0/output.json/products/<id>)"
        )

    async def fetch(self, url: str) -> PageResult:
        settings = get_settings()
        creds = (
            settings.cardmarket_app_token,
            settings.cardmarket_app_secret,
            settings.cardmarket_access_token,
            settings.cardmarket_access_secret,
        )
        if not all(creds):
            raise AdapterError(
                "cardmarket API credentials missing — set CARDMARKET_APP_TOKEN, "
                "CARDMARKET_APP_SECRET, CARDMARKET_ACCESS_TOKEN, CARDMARKET_ACCESS_SECRET in .env"
            )
        api_url = f"{API_BASE}/products/{self.product_id(url)}"
        auth_header = build_oauth1_header("GET", api_url, *creds)
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            try:
                resp = await client.get(api_url, headers={"Authorization": auth_header})
            except httpx.HTTPError as exc:
                raise AdapterError(f"cardmarket API request failed: {exc}") from exc
        json_data = None
        if "json" in resp.headers.get("content-type", ""):
            try:
                json_data = resp.json()
            except ValueError:
                pass
        return PageResult(
            url=url,
            final_url=api_url,
            status_code=resp.status_code,
            text=resp.text,
            json_data=json_data,
            fetched_via="cardmarket-api",
        )

    def parse(self, page: PageResult) -> StockResult:
        if page.status_code == 404:
            return StockResult(status=StockStatus.UNKNOWN, listed=False, note="product not found")
        if page.status_code == 401:
            raise AdapterError("cardmarket API: 401 unauthorized (check OAuth credentials)")
        if page.status_code == 429:
            raise AdapterError("cardmarket API: request limit reached (max 5000/day)")
        if not isinstance(page.json_data, dict) or "product" not in page.json_data:
            raise AdapterError(f"cardmarket API: unexpected response (HTTP {page.status_code})")

        product = page.json_data["product"]
        count_articles = int(product.get("countArticles") or 0)
        min_articles = int(self.detection_config.get("min_articles", 1))
        guide = product.get("priceGuide") or {}
        price = None
        for key in ("LOW", "LOWEX", "TREND", "AVG", "SELL"):
            if guide.get(key) is not None:
                price = float(guide[key])
                break
        website_path = product.get("website") or ""
        buy_url = f"https://www.cardmarket.com{website_path}" if website_path else None
        image = product.get("image")
        if image and image.startswith("//"):
            image = f"https:{image}"
        return StockResult(
            status=(
                StockStatus.IN_STOCK if count_articles >= min_articles else StockStatus.OUT_OF_STOCK
            ),
            price=price,
            title=product.get("enName") or product.get("locName"),
            image_url=image,
            buy_url=buy_url,
            note=f"{count_articles} articles listed",
        )
