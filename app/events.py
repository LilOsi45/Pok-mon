"""Domain events emitted by the monitor and news subsystems."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.models import EventType, Game


@dataclass(slots=True)
class Event:
    type: EventType
    game: Game | None
    title: str
    message: str = ""
    url: str | None = None
    cart_url: str | None = None  # Shopify one-tap add-to-cart permalink
    image_url: str | None = None
    price: float | None = None
    currency: str | None = None
    # Cardmarket reference so the ping shows whether the shop price is a deal
    cardmarket_trend: float | None = None
    cardmarket_low: float | None = None
    cardmarket_url: str | None = None
    retailer: str | None = None
    watch_id: int | None = None
    news_id: int | None = None
    # Routing keys, e.g. ["stock:pokemon", "watch:12"] or ["news:one_piece"].
    routes: list[str] = field(default_factory=list)
    # Priority events mention @everyone and bypass quiet hours
    priority: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_stock_event(self) -> bool:
        return self.type in (EventType.NEW_LISTING, EventType.BACK_IN_STOCK)


def stock_routes(game: Game, channels: list[str] | None = None) -> list[str]:
    routes = [f"stock:{game.value}"]
    for ch in channels or []:
        routes.append(ch if ":" in ch else f"channel:{ch}")
    return routes


def news_routes(game: Game | None) -> list[str]:
    return [f"news:{game.value}"] if game else ["news:both"]


def route_matches(pattern: str, routes: list[str]) -> bool:
    """Match a notifier route pattern against an event's routes.

    Patterns: "*" (everything), "stock:*" (prefix wildcard), exact strings,
    and bare words ("pokemon") which match any route segment.
    """
    if pattern == "*":
        return True
    for route in routes:
        if pattern == route:
            return True
        if pattern.endswith(":*") and route.startswith(pattern[:-1]):
            return True
        if ":" not in pattern and pattern in route.split(":"):
            return True
    return False
