"""Reusable stock-detection strategies, ordered by reliability:

1. JSON-LD / schema.org offers (`availability: InStock`)
2. Add-to-cart / buy button present AND no sold-out phrases
3. Price present + page looks purchasable

Used by the GenericAdapter fallback and by shop adapters as building blocks.
"""

from __future__ import annotations

import json
import re

from selectolax.parser import HTMLParser

from app.models import StockStatus
from app.monitor.base import StockResult

# schema.org availability values that mean "buyable now"
IN_STOCK_AVAILABILITY = {
    "instock",
    "instoreonly",
    "limitedavailability",
    "onlineonly",
    "presale",
    "preorder",
    "backorder",
}
OUT_OF_STOCK_AVAILABILITY = {"outofstock", "soldout", "discontinued"}

SOLD_OUT_PHRASES = (
    # German
    "ausverkauft",
    "nicht verfügbar",
    "nicht lieferbar",
    "derzeit nicht auf lager",
    "zurzeit nicht verfügbar",
    "momentan nicht verfügbar",
    "aktuell nicht verfügbar",
    "vergriffen",
    "artikel ist leider ausverkauft",
    # English
    "sold out",
    "out of stock",
    "currently unavailable",
    "no longer available",
    "temporarily out of stock",
)

BUY_BUTTON_PATTERNS = (
    "in den warenkorb",
    "in den einkaufswagen",
    "jetzt kaufen",
    "jetzt vorbestellen",
    "vorbestellen",
    "add to cart",
    "add to basket",
    "buy now",
    "pre-order",
    "preorder",
)

BUY_BUTTON_SELECTORS = (
    "#add-to-cart-button",
    "#buy-now-button",
    "button[name='add']",
    "form[action*='cart'] button[type='submit']",
    "form[action*='warenkorb'] button[type='submit']",
    "button[data-testid*='add-to-cart']",
    "[class*='add-to-cart']",
    "[class*='addToCart']",
    "[id*='addToCart']",
)

_PRICE_RE = re.compile(
    r"(?:€|EUR)\s*(\d{1,4}(?:[.,]\d{3})*(?:[.,]\d{2})?)|"
    r"(\d{1,4}(?:[.,]\d{3})*(?:[.,]\d{2}))\s*(?:€|EUR)"
)


def parse_german_price(raw: str | float | int | None) -> float | None:
    """'1.234,56' -> 1234.56; '49,99' -> 49.99; '49.99' -> 49.99."""
    if raw is None:
        return None
    if isinstance(raw, int | float):
        return float(raw)
    s = raw.strip().replace("\xa0", " ")
    m = _PRICE_RE.search(s)
    if m:
        s = m.group(1) or m.group(2)
    s = re.sub(r"[^\d,.]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):  # 1.234,56 (German)
            s = s.replace(".", "").replace(",", ".")
        else:  # 1,234.56 (English)
            s = s.replace(",", "")
    elif "," in s:
        # comma as decimal separator when followed by exactly 2 digits
        s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) else s.replace(",", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def extract_price_from_text(text: str) -> float | None:
    m = _PRICE_RE.search(text)
    return parse_german_price(m.group(0)) if m else None


# ---------------------------------------------------------------------------
# JSON-LD
# ---------------------------------------------------------------------------


def _iter_jsonld_nodes(doc: dict | list):
    if isinstance(doc, list):
        for item in doc:
            yield from _iter_jsonld_nodes(item)
    elif isinstance(doc, dict):
        yield doc
        for key in ("@graph", "itemListElement", "mainEntity"):
            if key in doc:
                yield from _iter_jsonld_nodes(doc[key])


def _normalize_availability(value: str | None) -> StockStatus | None:
    if not value:
        return None
    token = value.rsplit("/", 1)[-1].rsplit(":", 1)[-1].strip().lower()
    if token in IN_STOCK_AVAILABILITY:
        return StockStatus.IN_STOCK
    if token in OUT_OF_STOCK_AVAILABILITY:
        return StockStatus.OUT_OF_STOCK
    return None


def parse_json_ld(html: str) -> StockResult | None:
    """Extract a StockResult from schema.org Product/Offer JSON-LD blocks."""
    tree = HTMLParser(html)
    for node in tree.css("script[type='application/ld+json']"):
        raw = node.text(strip=False)
        if not raw:
            continue
        try:
            doc = json.loads(raw.strip())
        except json.JSONDecodeError:
            try:  # some shops embed invalid trailing commas / control chars
                doc = json.loads(re.sub(r"[\x00-\x1f]", " ", raw))
            except json.JSONDecodeError:
                continue
        for item in _iter_jsonld_nodes(doc):
            types = item.get("@type", "")
            types = [types] if isinstance(types, str) else types
            if "Product" not in types:
                continue
            offers = item.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            availability = _normalize_availability(
                offers.get("availability") if isinstance(offers, dict) else None
            )
            if availability is None:
                continue
            price = None
            currency = None
            if isinstance(offers, dict):
                price = parse_german_price(offers.get("price"))
                if price is None and isinstance(offers.get("priceSpecification"), dict):
                    price = parse_german_price(offers["priceSpecification"].get("price"))
                currency = offers.get("priceCurrency") or "EUR"
            image = item.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            if isinstance(image, dict):
                image = image.get("url")
            return StockResult(
                status=availability,
                price=price,
                currency=currency,
                title=item.get("name"),
                image_url=image,
                buy_url=(offers.get("url") if isinstance(offers, dict) else None),
                note="json-ld",
            )
    return None


# ---------------------------------------------------------------------------
# Heuristics: buy button / sold-out phrases / price
# ---------------------------------------------------------------------------


def find_sold_out_phrase(text_lower: str) -> str | None:
    for phrase in SOLD_OUT_PHRASES:
        if phrase in text_lower:
            return phrase
    return None


def find_buy_button(tree: HTMLParser) -> bool:
    for selector in BUY_BUTTON_SELECTORS:
        node = tree.css_first(selector)
        if node is not None and "disabled" not in (node.attributes or {}):
            return True
    for node in tree.css("button, input[type='submit'], a[role='button']"):
        attrs = node.attributes or {}
        if "disabled" in attrs:
            continue
        label = " ".join(
            filter(None, [node.text(deep=True, strip=True), attrs.get("value"), attrs.get("title")])
        ).lower()
        if any(pat in label for pat in BUY_BUTTON_PATTERNS):
            return True
    return False


def detect_stock(html: str, *, page_title: str | None = None) -> StockResult:
    """Combined heuristic detection for shops without a dedicated adapter."""
    jsonld = parse_json_ld(html)
    if jsonld is not None:
        return jsonld

    tree = HTMLParser(html)
    for tag in ("script", "style", "noscript"):
        for node in tree.css(tag):
            node.decompose()
    text_lower = (tree.body.text(separator=" ") if tree.body else "").lower()
    title = page_title
    if not title and (node := tree.css_first("title")):
        title = node.text(strip=True)

    sold_out = find_sold_out_phrase(text_lower)
    has_buy_button = find_buy_button(tree)
    price = extract_price_from_text(text_lower)

    if has_buy_button and not sold_out:
        return StockResult(status=StockStatus.IN_STOCK, price=price, title=title, note="buy-button")
    if sold_out:
        return StockResult(
            status=StockStatus.OUT_OF_STOCK,
            price=price,
            title=title,
            note=f"sold-out phrase: {sold_out!r}",
        )
    if price is not None and has_buy_button:
        return StockResult(status=StockStatus.IN_STOCK, price=price, title=title, note="price")
    return StockResult(status=StockStatus.UNKNOWN, price=price, title=title, note="no signal")
