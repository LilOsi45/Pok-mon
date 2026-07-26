"""One catalog request per shop instead of one request per product.

Shopify stores publish their whole catalogue — including the per-variant
`available` flag — at /products.json. With 18 watches on a single shop that is
18 separate fetches per cycle, which both hammers the shop and (because of the
per-domain politeness gap) makes the configured interval impossible to keep.

Fetching the catalogue once and serving every watch on that shop from it turns
those 18 requests into 1, and every product gets checked as often as the
catalogue is refreshed — faster *and* far gentler than the per-product path.

Shops that are not Shopify (or hide the endpoint) are remembered as such so we
don't probe them again on every check.
"""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlsplit

from app.config import get_settings

log = logging.getLogger(__name__)

CATALOG_PATH = "/products.json?limit=250"
NEGATIVE_TTL_SECONDS = 3600  # don't re-probe a non-Shopify shop every check

# host -> (fetched_at, {handle: product} | None)   None = not a Shopify catalog
_cache: dict[str, tuple[float, dict[str, dict] | None]] = {}
_locks: dict[str, asyncio.Lock] = {}


def clear_cache() -> None:
    _cache.clear()
    _locks.clear()


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


async def _load(base: str) -> dict[str, dict] | None:
    from app.monitor import fetchers

    try:
        page = await fetchers.fetch_httpx(f"{base}{CATALOG_PATH}", respect_robots=False)
    except Exception as exc:
        log.info("catalog fetch failed for %s: %s", base, exc)
        return None
    if page.status_code != 200:
        return None
    data = page.json_data
    if data is None and page.text:
        import json

        try:
            data = json.loads(page.text)
        except ValueError:
            return None
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        return None
    index = _index(data["products"])
    log.info("catalog for %s: %d products", base, len(index))
    return index or None


async def catalog(url: str) -> dict[str, dict] | None:
    """Cached {handle: product} for the shop behind `url`, or None."""
    split = urlsplit(url)
    if not split.scheme or not split.netloc:
        return None
    host = split.netloc
    base = f"{split.scheme}://{host}"
    ttl = get_settings().catalog_ttl_seconds
    now = time.monotonic()

    hit = _cache.get(host)
    if hit is not None:
        fetched_at, index = hit
        age = now - fetched_at
        if age < (ttl if index is not None else NEGATIVE_TTL_SECONDS):
            return index

    lock = _locks.setdefault(host, asyncio.Lock())
    async with lock:
        # another waiter may have refreshed it while we queued
        hit = _cache.get(host)
        if hit is not None and time.monotonic() - hit[0] < ttl and hit[1] is not None:
            return hit[1]
        index = await _load(base)
        _cache[host] = (time.monotonic(), index)
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
