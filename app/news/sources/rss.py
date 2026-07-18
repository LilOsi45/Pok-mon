"""Generic RSS/Atom news source (config-driven) — used for community feeds
like PokéBeach and any release-calendar feed that offers RSS."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, date, datetime
from time import mktime

import feedparser

from app.models import Game
from app.news.base import NewsItem, NewsSource, NewsSourceError

log = logging.getLogger(__name__)

# Item is only interesting when the title/summary mentions set/product news
DEFAULT_KEYWORDS = (
    "set",
    "expansion",
    "booster",
    "elite trainer",
    "starter deck",
    "collection",
    "display",
    "erweiterung",
    "produkte",
    "preorder",
    "pre-order",
    "vorbestell",
    "release",
    "launch",
    "reveal",
    "announc",
)

_DATE_RE = re.compile(
    r"(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
    re.IGNORECASE,
)
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        "january february march april may june july august september october november december".split()
    )
}


def guess_release_date(text: str) -> date | None:
    """Best-effort 'launches March 27, 2026' style extraction from text."""
    if m := _DATE_RE.search(text or ""):
        try:
            return date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
        except ValueError:
            return None
    return None


class RSSSource(NewsSource):
    type = "rss"

    def _game_for(self, text: str) -> Game | None:
        cfg_game = self.config.game
        if cfg_game == "pokemon":
            return Game.POKEMON
        if cfg_game == "one_piece":
            return Game.ONE_PIECE
        # "both": classify per item
        lowered = text.lower()
        if "one piece" in lowered:
            return Game.ONE_PIECE
        if "pokemon" in lowered or "pokémon" in lowered:
            return Game.POKEMON
        return None

    def _relevant(self, text: str) -> bool:
        lowered = text.lower()
        keywords = tuple(k.lower() for k in self.config.keywords) or DEFAULT_KEYWORDS
        return any(k in lowered for k in keywords)

    async def fetch_items(self) -> list[NewsItem]:
        from app.monitor.fetchers import fetch_httpx

        page = await fetch_httpx(
            self.config.url, respect_robots=False
        )  # feeds are meant to be fetched
        if page.status_code != 200:
            raise NewsSourceError(f"{self.name}: HTTP {page.status_code}")
        feed = await asyncio.to_thread(feedparser.parse, page.text)
        if feed.bozo and not feed.entries:
            raise NewsSourceError(f"{self.name}: unparseable feed ({feed.bozo_exception})")

        items: list[NewsItem] = []
        for entry in feed.entries[:50]:
            title = getattr(entry, "title", "") or ""
            summary = re.sub(r"<[^>]+>", " ", getattr(entry, "summary", "") or "")[:1000]
            blob = f"{title} {summary}"
            game = self._game_for(blob)
            if game is None or not self._relevant(blob):
                continue
            url = getattr(entry, "link", "") or self.config.url
            published_at = None
            if parsed := getattr(entry, "published_parsed", None):
                published_at = datetime.fromtimestamp(mktime(parsed), tz=UTC).replace(tzinfo=None)
            image = None
            for media in getattr(entry, "media_content", []) or []:
                if media.get("url"):
                    image = media["url"]
                    break
            if image is None:
                for enc in getattr(entry, "enclosures", []) or []:
                    if str(enc.get("type", "")).startswith("image/"):
                        image = enc.get("href")
                        break
            items.append(
                NewsItem(
                    game=game,
                    title=title.strip(),
                    url=url,
                    guid=getattr(entry, "id", None) or url,
                    source=self.name,
                    region=self.config.region,
                    summary=summary.strip() or None,
                    image_url=image,
                    release_date=guess_release_date(blob),
                    published_at=published_at,
                )
            )
        return items
