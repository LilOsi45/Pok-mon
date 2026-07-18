"""Curated catalog of major European Pokémon / One Piece retailers.

Selection criteria: well-known shops with an established Trustpilot profile
(50+ reviews, predominantly positive for the dedicated TCG stores) plus the
big mainstream retailers. Used to (a) render the dashboard "Shops" page and
(b) attach "search this product elsewhere" links to restock notifications.

Verified via Trustpilot (July 2026): Card Corner ~1.100 reviews, Games Island
~311, Fantasywelt ~390, Gate to the Games ~80, CardsRfun / TCG-Trade positive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote_plus, urlsplit

from app.models import Game

GOOGLE_SITE_SEARCH = "https://www.google.com/search?q=site%3A{domain}+{query}"


@dataclass(frozen=True)
class Shop:
    name: str
    domain: str
    url: str
    games: tuple[str, ...] = ("pokemon", "one_piece")
    # game value -> search URL template with {query}; "*" is the default.
    # Empty dict -> Google site-search fallback (always works).
    search_templates: dict[str, str] = field(default_factory=dict)
    trustpilot_url: str | None = None
    note: str = ""


SHOP_CATALOG: tuple[Shop, ...] = (
    # --- dedicated TCG shops (Trustpilot-checked) ---
    Shop(
        name="Card Corner",
        domain="card-corner.de",
        url="https://www.card-corner.de",
        trustpilot_url="https://de.trustpilot.com/review/www.card-corner.de",
        note="1.100+ Trustpilot-Bewertungen, stark bei japanischen Produkten",
    ),
    Shop(
        name="Games Island",
        domain="games-island.eu",
        url="https://www.games-island.eu",
        trustpilot_url="https://de.trustpilot.com/review/games-island.eu",
        note="300+ Trustpilot-Bewertungen, eigener Adapter im Tracker",
    ),
    Shop(
        name="Fantasywelt",
        domain="fantasywelt.de",
        url="https://www.fantasywelt.de",
        trustpilot_url="https://de.trustpilot.com/review/fantasywelt.de",
        note="390+ Trustpilot-Bewertungen, breites Sortiment",
    ),
    Shop(
        name="Gate to the Games",
        domain="gate-to-the-games.de",
        url="https://www.gate-to-the-games.de",
        trustpilot_url="https://de.trustpilot.com/review/www.gate-to-the-games.de",
        note="80+ Trustpilot-Bewertungen",
    ),
    Shop(
        name="CardsRfun",
        domain="cardsrfun.de",
        url="https://cardsrfun.de",
        trustpilot_url="https://de.trustpilot.com/review/cardsrfun.de",
        note="Offizielle Distributoren-Ware, sehr gute Bewertungen",
    ),
    Shop(
        name="TCG-Trade",
        domain="tcg-trade.de",
        url="https://www.tcg-trade.de",
        trustpilot_url="https://de.trustpilot.com/review/tcg-trade.de",
        note="Seit 2017, gute Trustpilot-Bewertungen",
    ),
    Shop(
        name="Cardmarket",
        domain="cardmarket.com",
        url="https://www.cardmarket.com",
        search_templates={
            "pokemon": "https://www.cardmarket.com/de/Pokemon/Products/Search?searchString={query}",
            "one_piece": "https://www.cardmarket.com/de/OnePiece/Products/Search?searchString={query}",
        },
        trustpilot_url="https://de.trustpilot.com/review/cardmarket.com",
        note="Größter EU-Marktplatz; API-Adapter im Tracker",
    ),
    # --- mainstream retailers ---
    Shop(
        name="Amazon.de",
        domain="amazon.de",
        url="https://www.amazon.de",
        search_templates={"*": "https://www.amazon.de/s?k={query}"},
        note="Eigener Adapter im Tracker",
    ),
    Shop(
        name="MediaMarkt",
        domain="mediamarkt.de",
        url="https://www.mediamarkt.de",
        search_templates={"*": "https://www.mediamarkt.de/de/search.html?query={query}"},
        note="Eigener Adapter im Tracker",
    ),
    Shop(
        name="Saturn",
        domain="saturn.de",
        url="https://www.saturn.de",
        search_templates={"*": "https://www.saturn.de/de/search.html?query={query}"},
        note="Eigener Adapter im Tracker",
    ),
    Shop(
        name="Müller",
        domain="mueller.de",
        url="https://www.mueller.de",
    ),
    Shop(
        name="Smyths Toys",
        domain="smythstoys.com",
        url="https://www.smythstoys.com/de/de-de",
        search_templates={"*": "https://www.smythstoys.com/de/de-de/search/?text={query}"},
    ),
    Shop(
        name="Kaufland.de",
        domain="kaufland.de",
        url="https://www.kaufland.de",
        search_templates={"*": "https://www.kaufland.de/s/?search_value={query}"},
    ),
)


def shops_for_game(game: Game | str | None) -> list[Shop]:
    value = game.value if isinstance(game, Game) else game
    return [s for s in SHOP_CATALOG if value is None or value in s.games]


def search_url(shop: Shop, query: str, game: Game | str | None = None) -> str:
    value = game.value if isinstance(game, Game) else (game or "*")
    template = shop.search_templates.get(value) or shop.search_templates.get("*")
    if not template:
        template = GOOGLE_SITE_SEARCH.format(domain=shop.domain, query="{query}")
    return template.format(query=quote_plus(query))


def other_shop_links(
    product_label: str,
    game: Game | str | None = None,
    exclude_url: str | None = None,
    limit: int = 8,
) -> list[tuple[str, str]]:
    """(name, search-url) pairs for all catalog shops except the triggering one.

    Attached to restock/new-listing alerts so every big shop is one click away.
    """
    exclude_domain = ""
    if exclude_url:
        exclude_domain = urlsplit(exclude_url).netloc.lower().removeprefix("www.")
    links: list[tuple[str, str]] = []
    for shop in shops_for_game(game):
        if exclude_domain and (
            shop.domain == exclude_domain or exclude_domain.endswith("." + shop.domain)
        ):
            continue
        links.append((shop.name, search_url(shop, product_label, game)))
        if len(links) >= limit:
            break
    return links
