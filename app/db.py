"""Async SQLAlchemy engine/session plus programmatic Alembic migrations."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import BASE_DIR, get_settings

log = logging.getLogger(__name__)

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        # The default pool (5 + 10 overflow) is too small once dozens of watches
        # poll on short intervals: a burst of checks took every connection and
        # the dashboard died with "QueuePool limit ... reached". Checks release
        # their connection before fetching now, but keep headroom anyway.
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
            pool_recycle=1800,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with get_sessionmaker()() as session:
        yield session


def sync_database_url(url: str | None = None) -> str:
    """Translate the async driver URL to a sync one for Alembic."""
    url = url or get_settings().database_url
    return (
        url.replace("+aiosqlite", "")
        .replace("+asyncpg", "+psycopg2")
        .replace("postgresql+psycopg2", "postgresql")
    )


def run_migrations() -> None:
    """Apply Alembic migrations up to head (called on startup)."""
    from alembic.config import Config

    from alembic import command

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", sync_database_url())
    command.upgrade(cfg, "head")
    log.info("database migrations applied (head)")


def reset_engine_for_tests() -> None:
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None


__all__ = [
    "get_engine",
    "get_session",
    "get_sessionmaker",
    "run_migrations",
    "sync_database_url",
    "reset_engine_for_tests",
    "Path",
]
