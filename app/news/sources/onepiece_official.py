"""Official One Piece Card Game (Bandai) news scraper.

Targets the information index of the English official site, e.g.
https://en.onepiece-cardgame.com/information/ — a server-rendered list where
each entry carries a date, a category tag (PRODUCTS / EVENTS / ...) and title.
Product-category entries are what we care about for new sets.
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

PRODUCT_MARKERS = ("product", "goods", "products")
SET_KEYWORDS = ("booster", "starter deck", "op-", "eb-", "st-", "prb-", "collection", "display")

_DATE_RE = re.compile(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})")


def _parse_date(text: str) -> datetime | None:
    if m := _DATE_RE.search(text):
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


class OnePieceOfficialSource(NewsSource):
    type = "onepiece_official"

    async def fetch_items(self) -> list[NewsItem]:
        from app.monitor.fetchers import fetch_httpx

        page = await fetch_httpx(self.config.url)
        if page.status_code != 200:
            raise NewsSourceError(f"{self.name}: HTTP {page.status_code}")
        tree = HTMLParser(page.text)

        items: list[NewsItem] = []
        seen: set[str] = set()
        for anchor in tree.css("a[href*='information'], ul li a, .newsList a, .infoList a"):
            attrs = anchor.attributes or {}
            href = attrs.get("href") or ""
            if not href or href.startswith(("javascript:", "#")):
                continue
            url = urljoin(page.final_url, href)
            if url in seen or url.rstrip("/").endswith("information"):
                continue
            text = re.sub(r"\s+", " ", anchor.text(deep=True, strip=True))
            if not text or len(text) < 8:
                continue
            lowered = text.lower()
            category_hit = any(marker in lowered for marker in PRODUCT_MARKERS)
            set_hit = any(kw in lowered for kw in SET_KEYWORDS)
            if not (category_hit or set_hit):
                continue
            seen.add(url)
            published_at = _parse_date(text)
            # Strip leading date / category tokens from the title
            title = _DATE_RE.sub("", text)
            title = re.sub(r"^\W+|^(?i:products?|goods|events?)\b\W*", "", title).strip()
            image = None
            if img := anchor.css_first("img"):
                image = urljoin(page.final_url, (img.attributes or {}).get("src") or "")
            items.append(
                NewsItem(
                    game=Game.ONE_PIECE,
                    title=title or text,
                    url=url,
                    guid=url,
                    source=self.name,
                    region=self.config.region,
                    image_url=image,
                    release_date=guess_release_date(text),
                    published_at=published_at,
                )
            )
        if not items:
            log.warning("%s: no product news entries parsed (layout change?)", self.name)
        return items
