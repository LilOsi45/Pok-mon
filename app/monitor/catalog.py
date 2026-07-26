"""One catalog request per shop instead of one request per product.

Shopify stores publish their whole catalogue — including the per-variant
`available` flag — at /products.json. With 18 watches on a single shop that is
18 separate fetches per cycle, which both hammers the shop and (because of the
per-domain politeness gap) makes the configured interval impossible to keep.

Fetching the catalogue once and serving every watch on that shop from it turns
those 18 requests into 1, and every product gets checked as often as the
catalogue is refreshed — faster *and* far gentler than the per-product path.

Failures are cached too, and the distinction matters: a 404 means the shop is
simply not Shopify and must not be probed again for an hour, while a 429 means
we asked too fast and should retry soon. Treating the second like the first
disables the catalogue for a whole hour on exactly the shops that need it most.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.config import get_settings

log = logging.getLogger(__name__)

CATALOG_PATH = "/products.json?limit=250"
NEGATIVE_TTL_SECONDS = 3600  # settled: not a Shopify shop, stop probing
RETRY_TTL_SECONDS = 120  # undecided: rate-limited or down, ask again soon


@dataclass(frozen=True)
class _Entry:
    at: float
    index: dict[str, dict] | None
    reason: str
    ttl: float

    def fresh(self, now: float) -> bool:
        return now - self.at < self.ttl


_cache: dict[str, _Entry] = {}
_locks: dict[str, asyncio.Lock] = {}


def clear_cache() -> None:
    _cache.clear()
    _locks.clear()


def reason(url_or_host: str) -> str | None:
    """Why the last probe of this shop did (not) yield a catalogue."""
    host = urlsplit(url_or_host).netloc or url_or_host
    entry = _cache.get(host)
    return None if entry is None else entry.reason


def product_handle(url: str) -> str | None:
    """Extract the Shopify product handle from /products/<handle>."""
    parts = [p for p in urlsplit(url).path.split("/") if p]
    if "products" not in parts:
        return None
    index = parts.index("products")
    if index + 1 >= len(parts):
        return None
    handle = parts[index + 1]
    for suffix in (".js", ".json"):
        if handle.endswith(suffix):
            handle = handle[: -len(suffix)]
    return handle or None


def _index(products: list) -> dict[str, dict]:
    return {
        product["handle"]: product
        for product in products
        if isinstance(product, dict) and isinstance(product.get("handle"), str)
    }


async def _load(base: str) -> tuple[dict[str, dict] | None, str, bool]:
    """Returns (index, human reason, transient).

    `transient` separates "ask again in two minutes" from "this shop has no
    catalogue, leave it alone for an hour".
    """
    from app.monitor import fetchers

    try:
        page = await fetchers.fetch_httpx(f"{base}{CATALOG_PATH}", respect_robots=False)
    except Exception as exc:
        log.info("catalog fetch failed for %s: %s", base, exc)
        return None, f"Abruf fehlgeschlagen ({type(exc).__name__})", True
    if page.status_code == 429:
        return None, "HTTP 429 — Shop drosselt uns", True
    if page.status_code >= 500:
        return None, f"HTTP {page.status_code} — Shop-Fehler", True
    if page.status_code != 200:
        return None, f"HTTP {page.status_code} — kein /products.json", False

    data = page.json_data
    if data is None and page.text:
        import json

        try:
            data = json.loads(page.text)
        except ValueError:
            return None, "Antwort ist kein JSON — kein Shopify-Shop", False
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        return None, "JSON ohne 'products' — kein Shopify-Shop", False

    index = _index(data["products"])
    if not index:
        return None, "Katalog ist leer", True
    log.info("catalog for %s: %d products", base, len(index))
    return index, f"{len(index)} Produkte", False


async def catalog(url: str) -> dict[str, dict] | None:
    """Cached {handle: product} for the shop behind `url`, or None."""
    split = urlsplit(url)
    if not split.scheme or not split.netloc:
        return None
    host = split.netloc
    base = f"{split.scheme}://{host}"
    now = time.monotonic()

    entry = _cache.get(host)
    if entry is not None and entry.fresh(now):
        return entry.index

    lock = _locks.setdefault(host, asyncio.Lock())
    async with lock:
        # Another waiter may have refreshed it while we queued. This must accept
        # a cached *failure* as well: with 18 watches on one shop, re-probing on
        # every miss meant 18 back-to-back catalogue requests, which is what
        # provoked the 429 that made them fail in the first place.
        entry = _cache.get(host)
        if entry is not None and entry.fresh(time.monotonic()):
            return entry.index

        index, why, transient = await _load(base)
        if index is not None:
            ttl = get_settings().catalog_ttl_seconds
        else:
            ttl = RETRY_TTL_SECONDS if transient else NEGATIVE_TTL_SECONDS
        _cache[host] = _Entry(at=time.monotonic(), index=index, reason=why, ttl=ttl)
        return index


async def product_from_catalog(url: str) -> dict | None:
    """The product dict for this URL, served from the shop's cached catalogue."""
    handle = product_handle(url)
    if handle is None:
        return None
    index = await catalog(url)
    if not index:
        return None
    return index.get(handle)
