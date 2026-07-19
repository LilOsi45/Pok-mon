"""SQLAlchemy ORM models."""

from __future__ import annotations

import enum
from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # store naive UTC


class Base(DeclarativeBase):
    pass


class Game(enum.StrEnum):
    POKEMON = "pokemon"
    ONE_PIECE = "one_piece"


class StockStatus(enum.StrEnum):
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    UNKNOWN = "UNKNOWN"


class EventType(enum.StrEnum):
    NEW_LISTING = "NEW_LISTING"
    BACK_IN_STOCK = "BACK_IN_STOCK"
    PRICE_DROP = "PRICE_DROP"
    NEW_SET_ANNOUNCED = "NEW_SET_ANNOUNCED"
    PREORDER_LIVE = "PREORDER_LIVE"
    RELEASE_SOON = "RELEASE_SOON"
    HEARTBEAT = "HEARTBEAT"
    TEST = "TEST"


class NewsStatus(enum.StrEnum):
    ANNOUNCED = "announced"
    PREORDER = "preorder"
    RELEASED = "released"


class Watch(Base):
    __tablename__ = "watches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game: Mapped[Game] = mapped_column(Enum(Game), index=True)
    label: Mapped[str] = mapped_column(String(255))
    retailer: Mapped[str] = mapped_column(String(120), default="")
    url: Mapped[str] = mapped_column(Text)
    adapter: Mapped[str | None] = mapped_column(String(80), default=None)
    detection: Mapped[dict] = mapped_column(JSON, default=dict)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=1800)
    channels: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # True once we've observed the product listed at least once (drives NEW_LISTING)
    listing_seen: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_on_first_seen: Mapped[bool] = mapped_column(Boolean, default=False)
    # priority watches mention @everyone and bypass quiet hours
    priority: Mapped[bool] = mapped_column(Boolean, default=False)
    # price alert: ping once when in stock at/below this price (rearms above it)
    price_target: Mapped[float | None] = mapped_column(Float, default=None)
    price_target_hit: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_status: Mapped[StockStatus] = mapped_column(Enum(StockStatus), default=StockStatus.UNKNOWN)
    last_price: Mapped[float | None] = mapped_column(Float, default=None)
    last_currency: Mapped[str | None] = mapped_column(String(8), default=None)
    last_title: Mapped[str | None] = mapped_column(Text, default=None)
    last_image_url: Mapped[str | None] = mapped_column(Text, default=None)
    last_buy_url: Mapped[str | None] = mapped_column(Text, default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    last_in_stock_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    checks: Mapped[list[StockCheck]] = relationship(
        back_populates="watch", cascade="all, delete-orphan", lazy="noload"
    )


class StockCheck(Base):
    __tablename__ = "stock_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    status: Mapped[StockStatus] = mapped_column(Enum(StockStatus), default=StockStatus.UNKNOWN)
    price: Mapped[float | None] = mapped_column(Float, default=None)
    currency: Mapped[str | None] = mapped_column(String(8), default=None)
    title: Mapped[str | None] = mapped_column(Text, default=None)
    http_status: Mapped[int | None] = mapped_column(Integer, default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    watch: Mapped[Watch] = relationship(back_populates="checks")


class SetNews(Base):
    __tablename__ = "set_news"
    __table_args__ = (UniqueConstraint("source", "guid", name="uq_news_source_guid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game: Mapped[Game] = mapped_column(Enum(Game), index=True)
    title: Mapped[str] = mapped_column(Text)
    set_name: Mapped[str | None] = mapped_column(String(255), default=None)
    region: Mapped[str] = mapped_column(String(32), default="EU")
    release_date: Mapped[date | None] = mapped_column(Date, default=None)
    preorder_date: Mapped[date | None] = mapped_column(Date, default=None)
    source: Mapped[str] = mapped_column(String(120), index=True)
    guid: Mapped[str] = mapped_column(String(500))  # dedupe key within a source (url or feed guid)
    url: Mapped[str] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text, default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[NewsStatus] = mapped_column(Enum(NewsStatus), default=NewsStatus.ANNOUNCED)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    announced_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    preorder_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    release_soon_notified: Mapped[bool] = mapped_column(Boolean, default=False)


class ProductScan(Base):
    """Keyword scanner: watches a listing page (category/search/new-arrivals)
    and fires when a NEW product matching the keywords appears."""

    __tablename__ = "product_scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game: Mapped[Game] = mapped_column(Enum(Game), index=True)
    label: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)  # listing page to scan
    keywords: Mapped[list] = mapped_column(JSON, default=list)  # ALL must match
    exclude_keywords: Mapped[list] = mapped_column(JSON, default=list)  # ANY excludes
    interval_seconds: Mapped[int] = mapped_column(Integer, default=900)
    channels: Mapped[list] = mapped_column(JSON, default=list)
    use_playwright: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # first successful check records what's already there without notifying
    baseline_done: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    items: Mapped[list[ScanItem]] = relationship(
        back_populates="scan", cascade="all, delete-orphan", lazy="noload"
    )


class ScanItem(Base):
    """A product URL a scanner has already seen (dedupe + hit history)."""

    __tablename__ = "scan_items"
    __table_args__ = (UniqueConstraint("scan_id", "url_key", name="uq_scan_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[int] = mapped_column(
        ForeignKey("product_scans.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(Text)
    url_key: Mapped[str] = mapped_column(String(500))  # normalized dedupe key
    title: Mapped[str] = mapped_column(Text)
    price: Mapped[float | None] = mapped_column(Float, default=None)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)

    scan: Mapped[ProductScan] = relationship(back_populates="items")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    event_type: Mapped[EventType] = mapped_column(Enum(EventType), index=True)
    notifier: Mapped[str] = mapped_column(String(120))  # notifier name
    notifier_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text, default=None)
    url: Mapped[str | None] = mapped_column(Text, default=None)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    watch_id: Mapped[int | None] = mapped_column(
        ForeignKey("watches.id", ondelete="SET NULL"), default=None
    )
    news_id: Mapped[int | None] = mapped_column(
        ForeignKey("set_news.id", ondelete="SET NULL"), default=None
    )
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class NotifierConfig(Base):
    """Dashboard-managed notifier targets (merged with config.yaml notifiers)."""

    __tablename__ = "notifier_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    type: Mapped[str] = mapped_column(String(32))  # discord | email | telegram
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    routes: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
