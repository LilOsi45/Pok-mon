"""Jinja2 environment with app-wide helpers/filters."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from app.config import get_settings


def local_dt(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if value is None:
        return "—"
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(ZoneInfo(get_settings().timezone)).strftime(fmt)


def ago(value: datetime | None) -> str:
    if value is None:
        return "never"
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    delta = datetime.now(UTC) - aware
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["local_dt"] = local_dt
templates.env.filters["ago"] = ago
