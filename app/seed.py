"""Seed file-configured watches into the DB on startup (idempotent)."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings, load_file_config
from app.models import Game, Watch

log = logging.getLogger(__name__)


async def seed_watches(session: AsyncSession) -> int:
    """Insert config.yaml watches that don't exist yet (matched by URL+label).

    Existing rows are left alone so dashboard edits survive restarts.
    """
    settings = get_settings()
    created = 0
    for seed in load_file_config().watches:
        existing = (
            await session.execute(
                select(Watch).where(Watch.url == seed.url, Watch.label == seed.label)
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(
            Watch(
                game=Game(seed.game),
                label=seed.label,
                retailer=seed.retailer,
                url=seed.url,
                adapter=seed.adapter,
                detection=seed.detection,
                interval_seconds=seed.interval_seconds or settings.default_poll_interval_seconds,
                cooldown_seconds=seed.cooldown_seconds or settings.default_cooldown_seconds,
                channels=seed.channels or [seed.game],
                enabled=seed.enabled,
            )
        )
        created += 1
    await session.commit()
    if created:
        log.info("seeded %d watch(es) from config file", created)
    return created
