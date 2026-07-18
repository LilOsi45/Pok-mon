"""Official Pokémon website news listing scraper.

Targets the pokemon.com news index (e.g. https://www.pokemon.com/us/pokemon-news
or the German https://www.pokemon.com/de/pokemon-news). The page is
server-rendered; each entry is an anchor tile with title, date and image.
Only TCG-related entries are kept.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from app.models import Game
from app.news.base import NewsItem, NewsSource, NewsSourceError
from app.news.sources.rss import guess_release_date

log = logging.getLogger(__name__)

TCG_MARKERS = (
    "tcg",
    "trading card",
    "sammelkartenspiel",
    "karmesin & purpur",
    "scarlet & violet",
    "mega evolution",
    "expansion",
    "erweiterung",
    "booster",
    "elite trainer",
    "top-trainer",
)

_DATE_FORMATS = ("%B %d, %Y", "%d. %B %Y", "%d.%m.%Y")


def _parse_date(text: str) -> datetime | None:
    text = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


class PokemonOfficialSource(NewsSource):
    type = "pokemon_official"

    async def fetch_items(self) -> list[NewsItem]:
        from app.monitor.fetchers import fetch_httpx

        page = await fetch_httpx(self.config.url)
        if page.status_code != 200:
            raise NewsSourceError(f"{self.name}: HTTP {page.status_code}")
        tree = HTMLParser(page.text)

        items: list[NewsItem] = []
        seen: set[str] = set()
        # News tiles are anchors linking under /pokemon-news/; be liberal in what
        # we accept so cosmetic layout changes don't break the source.
        for anchor in tree.css("a[href*='pokemon-news/'], a[href*='/news/']"):
            attrs = anchor.attributes or {}
            href = attrs.get("href") or ""
            url = urljoin(page.final_url, href)
            if url in seen or url.rstrip("/").endswith(("pokemon-news", "/news")):
                continue
            title = ""
            for selector in ("h3", "h2", ".news-list__title", "p"):
                if node := anchor.css_first(selector):
                    title = node.text(strip=True)
                    break
            if not title:
                title = anchor.text(deep=True, strip=True)[:200]
            if not title:
                continue
            blob = f"{title} {anchor.text(deep=True, strip=True)}".lower()
            if not any(marker in blob for marker in TCG_MARKERS):
                continue
            seen.add(url)
            image = None
            if img := anchor.css_first("img"):
                image = urljoin(page.final_url, (img.attributes or {}).get("src") or "")
            published_at = None
            if date_node := anchor.css_first("time, .date, [class*='date']"):
                published_at = _parse_date(date_node.text(strip=True))
            items.append(
                NewsItem(
                    game=Game.POKEMON,
                    title=re.sub(r"\s+", " ", title),
                    url=url,
                    guid=url,
                    source=self.name,
                    region=self.config.region,
                    image_url=image or None,
                    release_date=guess_release_date(blob),
                    published_at=published_at,
                )
            )
        if not items:
            log.warning("%s: page parsed but no TCG news tiles found (layout change?)", self.name)
        return items
