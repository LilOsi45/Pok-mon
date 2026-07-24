"""Cardmarket price reference for stock alerts.

A restock ping shows the shop's price — but "is 74.99 € a good deal?" needs a
second number. This module fetches the Cardmarket price guide for a product
the watch is explicitly linked to (``Watch.cardmarket_id``) and hands it to the
notifier.

Two deliberate design choices:

* **Explicit product id, no name guessing.** A wrong reference price is worse
  than none — it would push a buying decision the wrong way. Cardmarket lists
  sealed products separately per language, so pinning the id also pins the
  language: link the DE display to a DE watch and the price is right by
  construction. Use ``python -m app.cardmarket_find "<name>"`` to look ids up.
* **Never breaks a ping.** Every failure path returns None, so a Cardmarket
  outage, a missing key or a rate limit can never swallow a restock alert.

Results are cached (default 6 h) to stay far below the 5000 requests/day cap.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from app.config import get_settings
from app.monitor.oauth1 import build_oauth1_header

log = logging.getLogger(__name__)

API_BASE = "https://api.cardmarket.com/ws/v2.0/output.json"
CACHE_TTL_SECONDS = 6 * 3600

# Price guide keys, best reference first. TREND is what the market actually
# settles at; LOW is the cheapest listing (often a damaged/foreign copy).
TREND_KEYS = ("TREND", "AVG", "SELL", "AVG7", "AVG30")
LOW_KEYS = ("LOW", "LOWEX")


@dataclass(frozen=True, slots=True)
class PriceReference:
    id_product: int
    name: str
    trend: float | None
    low: float | None
    url: str | None

    @property
    def has_price(self) -> bool:
        return self.trend is not None or self.low is not None


_cache: dict[int, tuple[float, PriceReference | None]] = {}


def clear_cache() -> None:
    _cache.clear()


def _credentials() -> tuple[str, str, str, str] | None:
    s = get_settings()
    creds = (
        s.cardmarket_app_token,
        s.cardmarket_app_secret,
        s.cardmarket_access_token,
        s.cardmarket_access_secret,
    )
    return creds if all(creds) else None  # type: ignore[return-value]


def _pick(guide: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = guide.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _parse_product(id_product: int, product: dict) -> PriceReference:
    guide = product.get("priceGuide") or {}
    website = product.get("website") or ""
    return PriceReference(
        id_product=id_product,
        name=product.get("enName") or product.get("locName") or f"Produkt {id_product}",
        trend=_pick(guide, TREND_KEYS),
        low=_pick(guide, LOW_KEYS),
        url=f"https://www.cardmarket.com{website}" if website.startswith("/") else None,
    )


async def _request(path: str, params: dict[str, str] | None = None) -> dict | None:
    creds = _credentials()
    if creds is None:
        return None
    url = f"{API_BASE}{path}"
    request = httpx.Request("GET", url, params=params or {})
    # Cardmarket signs the full URL including the query string.
    header = build_oauth1_header("GET", str(request.url), *creds)
    try:
        async with httpx.AsyncClient(timeout=get_settings().request_timeout_seconds) as client:
            resp = await client.get(url, params=params or {}, headers={"Authorization": header})
    except httpx.HTTPError as exc:
        log.warning("cardmarket request failed (%s): %s", path, exc)
        return None
    if resp.status_code == 401:
        log.warning("cardmarket: 401 unauthorized — check the OAuth credentials in .env")
        return None
    if resp.status_code == 429:
        log.warning("cardmarket: daily request limit reached")
        return None
    if resp.status_code != 200:
        log.info("cardmarket %s -> HTTP %s", path, resp.status_code)
        return None
    try:
        return resp.json()
    except ValueError:
        log.warning("cardmarket: response was not JSON")
        return None


async def price_reference(id_product: int, *, use_cache: bool = True) -> PriceReference | None:
    """Price guide for one Cardmarket product. None when unavailable."""
    now = time.monotonic()
    if use_cache and (hit := _cache.get(id_product)) is not None:
        cached_at, value = hit
        if now - cached_at < CACHE_TTL_SECONDS:
            return value

    data = await _request(f"/products/{id_product}")
    product = data.get("product") if isinstance(data, dict) else None
    reference = _parse_product(id_product, product) if isinstance(product, dict) else None
    _cache[id_product] = (now, reference)
    return reference


async def find_products(search: str, *, id_game: int | None = None) -> list[PriceReference]:
    """Search Cardmarket by name — used by the id lookup helper."""
    params = {"search": search}
    if id_game is not None:
        params["idGame"] = str(id_game)
    data = await _request("/products/find", params)
    if not isinstance(data, dict):
        return []
    products = data.get("product") or []
    if isinstance(products, dict):  # single match is not wrapped in a list
        products = [products]
    out: list[PriceReference] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        try:
            pid = int(product.get("idProduct"))
        except (TypeError, ValueError):
            continue
        out.append(_parse_product(pid, product))
    return out
