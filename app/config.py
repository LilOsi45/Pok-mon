"""Application settings (.env) and file-based config (config.yaml)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Secrets and runtime settings, loaded from environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- core ---
    database_url: str = "sqlite+aiosqlite:///./data/tcg_tracker.db"
    config_file: str = "config.yaml"
    timezone: str = "Europe/Berlin"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    # --- dashboard auth ---
    secret_key: str = "change-me-please"
    dashboard_password: str = "change-me-please"
    # Optional bearer token for API access (Authorization: Bearer <token>)
    dashboard_token: str | None = None
    session_max_age_seconds: int = 60 * 60 * 24 * 14

    # --- notifications ---
    discord_webhook_url: str | None = None  # default webhook if config.yaml has none
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # --- fetching / politeness ---
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    proxy_url: str | None = None
    request_timeout_seconds: float = 25.0
    per_domain_concurrency: int = 1
    respect_robots_txt: bool = True

    # --- notifications: append search links to other big shops on restock alerts ---
    show_other_shops: bool = True

    # --- scheduling defaults ---
    default_poll_interval_seconds: int = 300  # 5 min
    default_cooldown_seconds: int = 1800  # 30 min between notifications per watch
    news_poll_interval_seconds: int = 1800  # 30 min
    release_soon_lead_days: int = 7

    # --- cardmarket API (OAuth 1.0a, https://api.cardmarket.com) ---
    cardmarket_app_token: str | None = None
    cardmarket_app_secret: str | None = None
    cardmarket_access_token: str | None = None
    cardmarket_access_secret: str | None = None

    @field_validator("database_url")
    @classmethod
    def _ensure_sqlite_dir(cls, v: str) -> str:
        if v.startswith("sqlite") and ":///" in v:
            path = v.split(":///", 1)[1]
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        return v


# ---------------------------------------------------------------------------
# config.yaml — watches seed, news sources, notifiers, routing
# ---------------------------------------------------------------------------


class WatchSeed(BaseModel):
    game: Literal["pokemon", "one_piece"]
    label: str
    retailer: str = ""
    url: str
    adapter: str | None = None  # force a specific adapter slug; default: resolve by domain
    interval_seconds: int | None = None
    cooldown_seconds: int | None = None
    channels: list[str] = Field(default_factory=list)  # routing keys, default: [game]
    detection: dict = Field(default_factory=dict)  # adapter-specific options
    enabled: bool = True


class NewsSourceConfig(BaseModel):
    name: str
    type: Literal["rss", "pokemon_official", "onepiece_official"]
    url: str
    game: Literal["pokemon", "one_piece", "both"] = "both"
    region: str = "EU"
    interval_seconds: int | None = None
    keywords: list[str] = Field(default_factory=list)  # extra filter keywords
    enabled: bool = True


class NotifierConfigModel(BaseModel):
    name: str
    type: Literal["discord", "email", "telegram"]
    # For discord: {"webhook_url": "..."} or {"webhook_url_env": "DISCORD_WEBHOOK_URL"}
    config: dict = Field(default_factory=dict)
    # Route patterns matched against event routes, e.g. ["stock:pokemon", "news:*", "*"]
    routes: list[str] = Field(default_factory=lambda: ["*"])
    enabled: bool = True


class FileConfig(BaseModel):
    watches: list[WatchSeed] = Field(default_factory=list)
    news_sources: list[NewsSourceConfig] = Field(default_factory=list)
    notifiers: list[NotifierConfigModel] = Field(default_factory=list)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_file_config(settings: Settings | None = None) -> FileConfig:
    settings = settings or get_settings()
    path = Path(settings.config_file)
    if not path.is_absolute():
        path = BASE_DIR / path
    if not path.exists():
        return FileConfig()
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    cfg = FileConfig.model_validate(raw)
    # Fallback: a bare DISCORD_WEBHOOK_URL env creates a default catch-all notifier.
    if not cfg.notifiers and (
        settings.discord_webhook_url or os.environ.get("DISCORD_WEBHOOK_URL")
    ):
        cfg.notifiers.append(
            NotifierConfigModel(
                name="default-discord",
                type="discord",
                config={"webhook_url_env": "DISCORD_WEBHOOK_URL"},
                routes=["*"],
            )
        )
    return cfg
