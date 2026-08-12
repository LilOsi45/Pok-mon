"""Keyword product scanner: watch a shop listing page (category / search /
new-arrivals) and notify when a NEW product matching the keywords appears.

Parsing strategy: schema.org JSON-LD ItemList/Product entries first, then a
generic anchor heuristic (product-looking links with a real title). The first
successful check records everything already on the page as a silent baseline;
afterwards every unseen matching product fires a notification. If a layout
change suddenly yields a flood of "new" items, one summary message is sent
instead of dozens of pings.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from selectolax.parser import HTMLParser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import Event, stock_routes
from app.models import EventType, ProductScan, ScanItem, utcnow
from app.monitor.detection import extract_price_from_text
from app.notify.service import dispatch_event

log = logging.getLogger(__name__)

FLOOD_CAP = 8  # more than this many new hits in one run -> one summary message
MIN_PRICE_DELTA = 0.5  # ignore sub-50-cent wobble to avoid parse-noise price pings

# path fragments that mark navigation/service links, never products
NAV_MARKERS = (
    "login",
    "account",
    "cart",
    "warenkorb",
    "checkout",
    "wishlist",
    "merkzettel",
    "impressum",
    "datenschutz",
    "agb",
    "widerruf",
    "kontakt",
    "newsletter",
    "versand",
    "zahlung",
    "hilfe",
    "faq",
    "jobs",
    "blog/",
    "/blog",
    "sitemap",
    "privacy",
    "terms",
    "about",
    "signin",
    "register",
)


@dataclass(slots=True)
class FoundProduct:
    url: str
    title: str
    price: float | None = None


def normalize_text(value: str) -> str:
    """Accent-insensitive lowercase ('Pokémon' matches 'pokemon')."""
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()


def url_key(url: str) -> str:
    """Dedupe key: scheme/host/path without query, fragment or trailing slash."""
    split = urlsplit(url)
    return urlunsplit((split.scheme, split.netloc.lower(), split.path.rstrip("/"), "", ""))[:500]


def keywords_match(title: str, keywords: list[str], exclude: list[str] | None = None) -> bool:
    text = normalize_text(title)
    if not all(normalize_text(k) in text for k in keywords if k.strip()):
        return False
    return not any(normalize_text(x) in text for x in (exclude or []) if x.strip())


# ---------------------------------------------------------------------------
# listing page parsing
# ---------------------------------------------------------------------------


def _jsonld_products(html: str, base_url: str) -> list[FoundProduct]:
    from app.monitor.detection import _iter_jsonld_nodes, parse_german_price

    tree = HTMLParser(html)
    products: list[FoundProduct] = []
    for node in tree.css("script[type='application/ld+json']"):
        raw = node.text(strip=False)
        if not raw:
            continue
        try:
            doc = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        for item in _iter_jsonld_nodes(doc):
            # ItemList entries either nest an `item` dict or carry url/name directly
            if "item" in item and isinstance(item["item"], dict):
                item = item["item"]
            types = item.get("@type", "")
            types = [types] if isinstance(types, str) else types
            if "Product" not in types:
                continue
            url = item.get("url") or item.get("@id") or ""
            name = item.get("name") or ""
            if not url or not name:
                continue
            offers = item.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = parse_german_price(offers.get("price")) if isinstance(offers, dict) else None
            products.append(
                FoundProduct(url=urljoin(base_url, url), title=name.strip(), price=price)
            )
    return products


PRODUCT_PATH_MARKERS = ("/product/", "/products/", "/produkt/", "/artikel/", "/dp/", "/p/")


def _looks_like_a_product_url(path_lower: str) -> bool:
    return any(marker in path_lower for marker in PRODUCT_PATH_MARKERS)


def _anchor_products(html: str, base_url: str) -> list[FoundProduct]:
    """Products from plain links, narrowed to real product URLs where possible.

    Blacklisting navigation paths does not scale: pokemoncenter.com's category
    page offered "Alle Neuerscheinungen shoppen", "30 Jahre Pokémon" and
    "LEGO® Pokémon™" as matches, all long enough to pass the title heuristic and
    all pointing at /category/ pages. Those would have pinged as drops, which is
    how an alert channel stops being believed.

    Blacklisting /category/ is not the answer either — Shopify products live at
    /collections/<x>/products/<y>. So when a page contains any link that looks
    like a product, only those count; pages with no such marker keep the old
    best-effort behaviour.
    """
    tree = HTMLParser(html)
    base_host = urlsplit(base_url).netloc.lower().removeprefix("www.")
    products: list[FoundProduct] = []
    product_like: list[FoundProduct] = []
    for anchor in tree.css("a[href]"):
        attrs = anchor.attributes or {}
        href = (attrs.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base_url, href)
        split = urlsplit(url)
        if split.netloc.lower().removeprefix("www.") != base_host:
            continue  # offsite links are never this shop's products
        path_lower = split.path.lower()
        if any(marker in path_lower for marker in NAV_MARKERS):
            continue
        title = anchor.text(deep=True, strip=True)
        if not title:
            if img := anchor.css_first("img"):
                title = ((img.attributes or {}).get("alt") or "").strip()
        title = re.sub(r"\s+", " ", title)
        if len(title) < 12:  # too short = nav/category link, not a product name
            continue
        price = None
        if parent := anchor.parent:
            price = extract_price_from_text(parent.text(deep=True, separator=" ")[:400])
        found = FoundProduct(url=url, title=title[:300], price=price)
        products.append(found)
        if _looks_like_a_product_url(path_lower):
            product_like.append(found)
    return product_like or products


def parse_listing(html: str, base_url: str) -> list[FoundProduct]:
    """Extract candidate products from a listing page, deduped by URL."""
    merged: dict[str, FoundProduct] = {}
    for product in _jsonld_products(html, base_url) + _anchor_products(html, base_url):
        key = url_key(product.url)
        existing = merged.get(key)
        if existing is None:
            merged[key] = product
        else:  # keep the richer record
            if existing.price is None and product.price is not None:
                existing.price = product.price
            if len(product.title) > len(existing.title):
                existing.title = product.title
    return list(merged.values())


# ---------------------------------------------------------------------------
# scan execution
# ---------------------------------------------------------------------------


def _hit_event(scan: ProductScan, item: ScanItem) -> Event:
    domain = urlsplit(item.url).netloc.removeprefix("www.")
    return Event(
        type=EventType.NEW_LISTING,
        game=scan.game,
        title=f"Neu entdeckt: {item.title}"[:250],
        message=f"„{scan.label}“ hat es auf {domain} gefunden.",
        url=item.url,
        price=item.price,
        retailer=domain,
        routes=stock_routes(scan.game, list(scan.channels or [])) + [f"scan:{scan.id}"],
        priority=scan.priority,
    )


def _summary_event(scan: ProductScan, items: list[ScanItem]) -> Event:
    lines = "\n".join(f"• [{i.title[:80]}]({i.url})" for i in items[:12])
    domain = urlsplit(scan.url).netloc.removeprefix("www.")
    return Event(
        type=EventType.NEW_LISTING,
        game=scan.game,
        title=f"{len(items)} neue Produkte bei {scan.label}"[:250],
        message=lines[:3900],
        url=scan.url,
        retailer=domain,
        routes=stock_routes(scan.game, list(scan.channels or [])) + [f"scan:{scan.id}"],
        priority=scan.priority,
    )


def _price_event(scan: ProductScan, item: ScanItem, old: float, new: float) -> Event:
    domain = urlsplit(item.url).netloc.removeprefix("www.")
    arrow = "📉 Preis gefallen" if new < old else "📈 Preis gestiegen"
    return Event(
        type=EventType.PRICE_DROP,
        game=scan.game,
        title=f"{arrow}: {item.title}"[:250],
        message=f"{old:.2f} € → **{new:.2f} €** auf {domain}",
        url=item.url,
        price=new,
        retailer=domain,
        routes=stock_routes(scan.game, list(scan.channels or [])) + [f"scan:{scan.id}"],
        priority=scan.priority,
    )


async def run_scan(session: AsyncSession, scan: ProductScan) -> list[ScanItem]:
    """Run one scanner pass. Commits. Never raises on fetch/parse errors."""
    from app.monitor import fetchers
    from app.monitor.registry import resolve_adapter

    # Release the pooled DB connection before fetching — a listing page through
    # the unlocker can take 30 s+, and holding a connection that long starves
    # the pool (see check_watch for the same fix).
    await session.commit()
    try:
        # Bot-protected chains (MediaMarkt/Saturn/Smyths…) resolve to a
        # scraperapi adapter — route the scan through the same unlocker.
        if resolve_adapter(scan.url).fetcher == "scraperapi":
            page = await fetchers.fetch_scraperapi(scan.url)
        elif scan.use_playwright:
            page = await fetchers.fetch_playwright(scan.url)
        else:
            page = await fetchers.fetch_httpx(scan.url)
        if page.status_code != 200:
            raise fetchers.FetchError(f"HTTP {page.status_code}")
        found = parse_listing(page.text, page.final_url)
    except Exception as exc:
        scan.last_check_at = utcnow()
        # The type matters: a bare message like "int() argument must be..."
        # says nothing about what actually broke.
        scan.last_error = f"{type(exc).__name__}: {exc}"[:500]
        await session.commit()
        log.warning("scan %s failed: %s", scan.id, exc)
        return []

    matching = [
        f
        for f in found
        if keywords_match(f.title, list(scan.keywords or []), list(scan.exclude_keywords or []))
    ]
    existing_items = (
        (await session.execute(select(ScanItem).where(ScanItem.scan_id == scan.id))).scalars().all()
    )
    existing_by_key = {i.url_key: i for i in existing_items}
    seen_this_run: set[str] = set()
    new_items: list[ScanItem] = []
    price_changes: list[tuple[ScanItem, float, float]] = []
    for product in matching:
        key = url_key(product.url)
        if key in seen_this_run:
            continue
        seen_this_run.add(key)
        existing = existing_by_key.get(key)
        if existing is None:
            new_items.append(
                ScanItem(
                    scan_id=scan.id,
                    url=product.url,
                    url_key=key,
                    title=product.title,
                    price=product.price,
                )
            )
        elif (
            product.price is not None
            and existing.price is not None
            and abs(product.price - existing.price) >= MIN_PRICE_DELTA
        ):
            price_changes.append((existing, existing.price, product.price))
            existing.price = product.price

    scan.last_check_at = utcnow()
    scan.last_error = None
    is_baseline = not scan.baseline_done
    if is_baseline:
        for item in new_items:
            item.notified = True  # record silently, don't spam on scanner creation
        scan.baseline_done = True
    session.add_all(new_items)
    await session.commit()
    log.info(
        "scan %s (%s): %d products, %d matching, %d new, %d price-changes%s",
        scan.id,
        scan.label,
        len(found),
        len(matching),
        len(new_items),
        len(price_changes),
        " (baseline)" if is_baseline else "",
    )
    if is_baseline:
        return new_items

    if new_items:
        if len(new_items) > FLOOD_CAP:
            await dispatch_event(session, _summary_event(scan, new_items))
        else:
            for item in new_items:
                await dispatch_event(session, _hit_event(scan, item))
        for item in new_items:
            item.notified = True

    if price_changes and len(price_changes) <= FLOOD_CAP:
        for item, old, new in price_changes:
            await dispatch_event(session, _price_event(scan, item, old, new))

    await session.commit()
    return new_items


async def run_scan_by_id(scan_id: int) -> None:
    """Scheduler entry point — own session per job run, bounded concurrency."""
    from app.db import get_sessionmaker
    from app.limits import job_slots

    async with job_slots():
        async with get_sessionmaker()() as session:
            scan = await session.get(ProductScan, scan_id)
            if scan is None or not scan.enabled:
                return
            await run_scan(session, scan)
