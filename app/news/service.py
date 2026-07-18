"""News polling: fetch sources, dedupe into set_news, emit news events."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import NewsSourceConfig, get_settings, load_file_config
from app.events import Event, news_routes
from app.models import EventType, NewsStatus, SetNews, utcnow
from app.news.base import NewsItem, NewsSource
from app.news.sources.onepiece_official import OnePieceOfficialSource
from app.news.sources.pokemon_official import PokemonOfficialSource
from app.news.sources.rss import RSSSource
from app.notify.service import dispatch_event

log = logging.getLogger(__name__)

SOURCE_CLASSES: dict[str, type[NewsSource]] = {
    "rss": RSSSource,
    "pokemon_official": PokemonOfficialSource,
    "onepiece_official": OnePieceOfficialSource,
}

PREORDER_MARKERS = ("pre-order", "preorder", "vorbestell", "pre order")


def build_source(config: NewsSourceConfig) -> NewsSource | None:
    cls = SOURCE_CLASSES.get(config.type)
    if cls is None:
        log.error("unknown news source type %r (%s)", config.type, config.name)
        return None
    return cls(config)


def _news_event(news: SetNews, event_type: EventType, message: str = "") -> Event:
    return Event(
        type=event_type,
        game=news.game,
        title=news.title if event_type == EventType.NEW_SET_ANNOUNCED else f"{news.title}",
        message=message or (news.summary or ""),
        url=news.url,
        image_url=news.image_url,
        news_id=news.id,
        routes=news_routes(news.game) + [f"source:{news.source}"],
    )


async def ingest_items(session: AsyncSession, items: list[NewsItem]) -> list[SetNews]:
    """Insert unseen items; return the newly created rows."""
    created: list[SetNews] = []
    for item in items:
        existing = (
            await session.execute(
                select(SetNews).where(SetNews.source == item.source, SetNews.guid == item.guid)
            )
        ).scalar_one_or_none()
        if existing is not None:
            # refresh dates if the source learned more (e.g. release date added)
            if item.release_date and existing.release_date != item.release_date:
                existing.release_date = item.release_date
            continue
        lowered = f"{item.title} {item.summary or ''}".lower()
        status = (
            NewsStatus.PREORDER
            if any(m in lowered for m in PREORDER_MARKERS)
            else NewsStatus.ANNOUNCED
        )
        news = SetNews(
            game=item.game,
            title=item.title[:500],
            set_name=item.set_name,
            region=item.region,
            release_date=item.release_date,
            preorder_date=item.preorder_date,
            source=item.source,
            guid=item.guid[:500],
            url=item.url,
            image_url=item.image_url,
            summary=item.summary,
            status=status,
            published_at=item.published_at,
        )
        session.add(news)
        created.append(news)
    await session.commit()
    return created


async def poll_source(session: AsyncSession, config: NewsSourceConfig) -> list[SetNews]:
    """Fetch one source, store new items, emit NEW_SET_ANNOUNCED / PREORDER_LIVE."""
    source = build_source(config)
    if source is None:
        return []
    try:
        items = await source.fetch_items()
    except Exception as exc:
        log.warning("news source %s failed: %s", config.name, exc, extra={"source": config.name})
        return []
    created = await ingest_items(session, items)
    for news in created:
        if news.status == NewsStatus.PREORDER and not news.preorder_notified:
            news.preorder_notified = True
            news.announced_notified = True  # don't double-announce
            await session.commit()
            await dispatch_event(
                session, _news_event(news, EventType.PREORDER_LIVE, "Preorders appear to be live.")
            )
        elif not news.announced_notified:
            news.announced_notified = True
            await session.commit()
            await dispatch_event(session, _news_event(news, EventType.NEW_SET_ANNOUNCED))
    if created:
        log.info("news source %s: %d new item(s)", config.name, len(created))
    return created


async def scan_release_soon(session: AsyncSession) -> None:
    """Daily scan: RELEASE_SOON for sets releasing within the lead window."""
    settings = get_settings()
    today = utcnow().date()
    horizon = today + timedelta(days=settings.release_soon_lead_days)
    rows = (
        (
            await session.execute(
                select(SetNews).where(
                    SetNews.release_date.is_not(None),
                    SetNews.release_date >= today,
                    SetNews.release_date <= horizon,
                    SetNews.release_soon_notified.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    for news in rows:
        news.release_soon_notified = True
        await session.commit()
        days = (news.release_date - today).days
        await dispatch_event(
            session,
            _news_event(
                news,
                EventType.RELEASE_SOON,
                f"Releases in {days} day(s) — {news.release_date.isoformat()}.",
            ),
        )


async def poll_source_by_name(name: str) -> None:
    """Scheduler entry point."""
    from app.db import get_sessionmaker

    config = next(
        (c for c in load_file_config().news_sources if c.name == name and c.enabled), None
    )
    if config is None:
        return
    async with get_sessionmaker()() as session:
        await poll_source(session, config)


async def run_release_soon_scan() -> None:
    from app.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        await scan_release_soon(session)
