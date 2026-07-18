from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Game, StockStatus, Watch

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as sess:
        yield sess
    await engine.dispose()


@pytest.fixture
def watch() -> Watch:
    return Watch(
        id=1,
        game=Game.POKEMON,
        label="Test product",
        retailer="Test Shop",
        url="https://example.com/product",
        interval_seconds=300,
        cooldown_seconds=1800,
        channels=["pokemon"],
        enabled=True,
        last_status=StockStatus.UNKNOWN,
        listing_seen=False,
        notify_on_first_seen=False,
        detection={},
    )
