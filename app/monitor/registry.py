"""Adapter registry: resolve a RetailerAdapter by domain or explicit slug."""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from app.monitor.adapters.amazon_de import AmazonDeAdapter
from app.monitor.adapters.cardmarket import CardmarketAdapter
from app.monitor.adapters.games_island import GamesIslandAdapter
from app.monitor.adapters.generic import GenericAdapter
from app.monitor.adapters.mediamarkt_de import MediaMarktDeAdapter, SaturnDeAdapter
from app.monitor.adapters.stubs import (
    BandaiStoreAdapter,
    GameStopDeAdapter,
    GamewareAdapter,
    KauflandAdapter,
    MuellerAdapter,
    PokemonCenterEuAdapter,
    SmythsToysDeAdapter,
)
from app.monitor.base import RetailerAdapter

log = logging.getLogger(__name__)

ADAPTER_CLASSES: tuple[type[RetailerAdapter], ...] = (
    AmazonDeAdapter,
    MediaMarktDeAdapter,
    SaturnDeAdapter,
    CardmarketAdapter,
    GamesIslandAdapter,
    MuellerAdapter,
    SmythsToysDeAdapter,
    GameStopDeAdapter,
    KauflandAdapter,
    GamewareAdapter,
    PokemonCenterEuAdapter,
    BandaiStoreAdapter,
    GenericAdapter,
)

BY_SLUG: dict[str, type[RetailerAdapter]] = {cls.slug: cls for cls in ADAPTER_CLASSES}
BY_DOMAIN: dict[str, type[RetailerAdapter]] = {
    domain: cls for cls in ADAPTER_CLASSES for domain in cls.domains
}


def resolve_adapter(
    url: str, slug: str | None = None, detection_config: dict | None = None
) -> RetailerAdapter:
    """Pick the adapter for a watch: explicit slug wins, then domain, then generic."""
    detection_config = detection_config or {}
    if slug:
        cls = BY_SLUG.get(slug)
        if cls is None:
            log.warning("unknown adapter slug %r, falling back to domain resolution", slug)
        else:
            return cls(detection_config)
    domain = urlsplit(url).netloc.lower()
    cls = BY_DOMAIN.get(domain) or BY_DOMAIN.get(domain.removeprefix("www."))
    if cls is None:
        # also match parent domains (shop.example.de -> example.de)
        parts = domain.split(".")
        for i in range(1, len(parts) - 1):
            if cls := BY_DOMAIN.get(".".join(parts[i:])):
                break
    if cls is None:
        cls = GenericAdapter
    adapter = cls(detection_config)
    # Per-watch fetcher override, e.g. {"fetcher": "playwright"}
    if fetcher := detection_config.get("fetcher"):
        adapter.fetcher = fetcher  # type: ignore[misc]
    return adapter
