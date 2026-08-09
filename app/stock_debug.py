"""Why does Holo read this product page the way it does?

    docker compose exec -T app python -m app.stock_debug https://shop.de/produkt

Prints every signal the generic detection looks at, and the buttons it did NOT
recognise. A shop reported "unklar" or, worse, pinged a product that could not
be ordered, and the only way to fix that was to guess at the shop's HTML from
the outside. The unrecognised-button list is the answer: it shows what this shop
calls its add-to-cart control, which is exactly what has to be added.

Contacts one shop, once. Sends nothing, changes nothing.
"""

from __future__ import annotations

import asyncio
import sys

from selectolax.parser import HTMLParser

from app.monitor.detection import (
    BUY_BUTTON_PATTERNS,
    BUY_BUTTON_SELECTORS,
    detect_stock,
    extract_price_from_text,
    find_buy_button,
    find_sold_out_phrase,
    parse_json_ld,
    parse_microdata,
)

MAX_BUTTONS = 15


def _label(node) -> str:
    attrs = node.attributes or {}
    text = " ".join(
        filter(
            None,
            [
                node.text(deep=True, strip=True),
                attrs.get("value"),
                attrs.get("title"),
                attrs.get("aria-label"),
            ],
        )
    )
    return " ".join(text.split())[:70]


def buttons(tree: HTMLParser) -> list[tuple[str, str]]:
    """(label, why it does or does not count) for every clickable thing."""
    found: list[tuple[str, str]] = []
    for node in tree.css("button, input[type='submit'], a[role='button']"):
        attrs = node.attributes or {}
        label = _label(node)
        if not label:
            continue
        if "disabled" in attrs:
            found.append((label, "ausgegraut — zählt nicht"))
        elif any(pat in label.lower() for pat in BUY_BUTTON_PATTERNS):
            found.append((label, "ERKANNT als Kaufen-Knopf"))
        else:
            found.append((label, "nicht erkannt"))
    return found[:MAX_BUTTONS]


def _fetch_line(page) -> list[str]:
    """Status, landing URL and route — the facts that tell a 404 page, a consent
    wall and a JavaScript shell apart. Leaving them out cost a round of
    diagnosis: 40 KB with no buttons looked like "JS-rendered" when the same
    shop had already served 509 KB with a readable price on another page."""
    lines = [
        f"HTTP  : {page.status_code}",
        f"Weg   : {page.fetched_via}",
    ]
    if page.final_url != page.url:
        lines.append(f"Gelandet auf: {page.final_url}")
    return lines


def report(url: str, html: str) -> str:
    tree = HTMLParser(html)
    for tag in ("script", "style", "noscript"):
        for node in tree.css(tag):
            node.decompose()
    text_lower = (tree.body.text(separator=" ") if tree.body else "").lower()

    jsonld = parse_json_ld(html)
    micro = parse_microdata(HTMLParser(html))
    sold_out = find_sold_out_phrase(text_lower)
    result = detect_stock(html)

    out = [
        f"URL   : {url}",
        f"Größe : {len(html)} Zeichen",
        "",
        "SIGNALE",
        f"  JSON-LD           : {jsonld.status.value if jsonld else 'keins'}",
        f"  Microdata         : {micro.value if micro else 'keine'}",
        f"  Ausverkauft-Satz  : {sold_out!r}" if sold_out else "  Ausverkauft-Satz  : keiner",
        f"  Kaufen-Knopf      : {'ja' if find_buy_button(tree) else 'NEIN'}",
        f"  Preis             : {extract_price_from_text(text_lower)}",
        "",
        f"URTEIL: {result.status.value}  ({result.note})",
        "",
        "KNÖPFE AUF DER SEITE",
    ]
    rows = buttons(tree)
    if rows:
        out += [f"  {why:26}  {label}" for label, why in rows]
    else:
        out.append("  keine gefunden — die Seite baut ihre Knöpfe wohl per JavaScript")
    out += [
        "",
        f"Als Kaufen-Knopf gelten Beschriftungen mit: {', '.join(BUY_BUTTON_PATTERNS)}",
        f"…oder diese Stellen im HTML: {', '.join(BUY_BUTTON_SELECTORS)}",
    ]
    return "\n".join(out)


async def _run(url: str) -> None:
    from app.monitor import fetchers

    try:
        try:
            page = await fetchers.fetch_httpx(url)
            print("\n".join(_fetch_line(page)))
            print(report(url, page.text))
        except Exception as exc:
            print(f"Normaler Abruf fehlgeschlagen: {type(exc).__name__}: {exc}")

        # Always compare against a real browser. "No buttons" has two very
        # different causes — the shop builds them with JavaScript, or we never
        # got the product page at all — and only the comparison separates them.
        print("\n" + "=" * 64)
        print("ZUM VERGLEICH: echter Browser (Playwright)\n")
        try:
            page = await fetchers.fetch_playwright(url)
            print("\n".join(_fetch_line(page)))
            print(report(url, page.text))
        except Exception as exc:
            print(f"Browser-Abruf fehlgeschlagen: {type(exc).__name__}: {exc}")
    finally:
        await fetchers.shutdown_playwright()
        await fetchers.close_client()


def main() -> None:
    if len(sys.argv) != 2:
        print("Aufruf: python -m app.stock_debug <produkt-url>")
        raise SystemExit(2)
    asyncio.run(_run(sys.argv[1]))


if __name__ == "__main__":
    main()
