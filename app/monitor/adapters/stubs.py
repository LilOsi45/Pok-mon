"""Stub adapters — registered by domain, awaiting a real `parse` implementation.

Each stub currently falls back to the generic detection pipeline so watches
against these shops still *work* in a best-effort way, but emits a note so you
can see it in check logs. Replace `parse` with shop-specific logic (see the
TODO on each class). Amazon/MediaMarkt/Cardmarket/Games Island are the
reference implementations.
"""

from __future__ import annotations

import logging

from app.monitor.adapters.generic import GenericAdapter
from app.monitor.base import PageResult, StockResult

log = logging.getLogger(__name__)


class _BestEffortStub(GenericAdapter):
    """Generic detection + a 'stub' marker in the result note.

    These are all big retail chains that render stock via JavaScript and/or sit
    behind bot protection, so they default to the scraping API. Without a
    SCRAPER_API_KEY that fetcher transparently falls back to plain httpx.
    """

    fetcher = "scraperapi"

    def parse(self, page: PageResult) -> StockResult:
        result = super().parse(page)
        result.note = f"stub-adapter({self.slug}): {result.note}"
        return result


class MuellerAdapter(_BestEffortStub):
    # TODO: mueller.de exposes JSON-LD on product pages; verify availability
    # values and add store-pickup handling.
    slug = "mueller"
    name = "Müller"
    domains = ("mueller.de", "www.mueller.de")


class SmythsToysDeAdapter(_BestEffortStub):
    # TODO: smythstoys.com/de/de-de — check the `productAvailability` JSON blob
    # embedded in the page; JSON-LD is present but sometimes stale.
    slug = "smyths_de"
    name = "Smyths Toys DE"
    domains = ("smythstoys.com", "www.smythstoys.com")


class GameStopDeAdapter(_BestEffortStub):
    # TODO: gamestop.de product pages expose availability in a dataLayer JSON
    # blob; parse that instead of text heuristics.
    slug = "gamestop_de"
    name = "GameStop.de"
    domains = ("gamestop.de", "www.gamestop.de")


class KauflandAdapter(_BestEffortStub):
    # TODO: kaufland.de is a marketplace — one product has many offers. Parse
    # the offer list (JSON in `window.__PRELOADED_STATE__`) and pick the
    # cheapest new offer; JSON-LD only covers the buybox winner.
    slug = "kaufland"
    name = "Kaufland.de"
    domains = ("kaufland.de", "www.kaufland.de")


class GamewareAdapter(_BestEffortStub):
    # TODO: gameware.at ships schema.org microdata (itemprop=availability);
    # parse the link href (schema.org/InStock) for a precise signal.
    slug = "gameware"
    name = "Gameware.at"
    domains = ("gameware.at", "www.gameware.at")


class PokemonCenterEuAdapter(_BestEffortStub):
    # TODO: pokemoncenter.com (EU storefront) sits behind aggressive bot
    # protection (Imperva). Needs fetcher="playwright", a warmed-up context and
    # likely residential proxies; availability lives in the Next.js data blob.
    slug = "pokemon_center_eu"
    name = "Pokémon Center EU"
    domains = ("pokemoncenter.com", "www.pokemoncenter.com")


class BandaiStoreAdapter(_BestEffortStub):
    # TODO: One Piece Card Game official sales run through p-bandai.eu /
    # premium-bandai; product pages are JS-rendered — keep playwright and parse
    # the add-to-cart state + "SOLD OUT" ribbon.
    slug = "bandai_store"
    name = "Premium Bandai EU"
    domains = ("p-bandai.eu", "www.p-bandai.eu", "p-bandai.com")
