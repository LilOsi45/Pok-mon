"""News source interface and normalized item type."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime

from app.config import NewsSourceConfig
from app.models import Game


class NewsSourceError(Exception):
    pass


@dataclass(slots=True)
class NewsItem:
    """One normalized news entry, before dedupe/persistence."""

    game: Game
    title: str
    url: str
    guid: str  # stable dedupe key within the source (feed guid or url)
    source: str
    set_name: str | None = None
    region: str = "EU"
    summary: str | None = None
    image_url: str | None = None
    release_date: date | None = None
    preorder_date: date | None = None
    published_at: datetime | None = None


class NewsSource(ABC):
    """One configured news origin (a feed or a scraped page)."""

    type: str = "base"

    def __init__(self, config: NewsSourceConfig) -> None:
        self.config = config
        self.name = config.name

    @abstractmethod
    async def fetch_items(self) -> list[NewsItem]:
        """Fetch and normalize current items. Raise NewsSourceError on failure."""
