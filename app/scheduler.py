"""APScheduler wiring: one interval job per enabled watch + news source,
plus a daily RELEASE_SOON scan. Job failures never kill the scheduler."""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.config import get_settings, load_file_config
from app.models import Watch

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(
            timezone=ZoneInfo(get_settings().timezone),
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 120},
        )
    return _scheduler


def schedule_watch(watch: Watch) -> None:
    """Add or replace the polling job for a watch."""
    from app.monitor.service import check_watch_by_id

    scheduler = get_scheduler()
    job_id = f"watch:{watch.id}"
    if not watch.enabled:
        remove_watch_job(watch.id)
        return
    interval = max(60, watch.interval_seconds or get_settings().default_poll_interval_seconds)
    scheduler.add_job(
        check_watch_by_id,
        IntervalTrigger(seconds=interval, jitter=int(interval * 0.2)),
        args=[watch.id],
        id=job_id,
        replace_existing=True,
        name=f"watch {watch.id}: {watch.label}",
    )
    log.info("scheduled watch %s every %ss (±20%% jitter)", watch.id, interval)


def remove_watch_job(watch_id: int) -> None:
    job_id = f"watch:{watch_id}"
    if get_scheduler().get_job(job_id):
        get_scheduler().remove_job(job_id)


async def sync_watch_jobs() -> None:
    """Reconcile scheduler jobs with the watches table (called on startup and
    after dashboard changes)."""
    from app.db import get_sessionmaker

    scheduler = get_scheduler()
    async with get_sessionmaker()() as session:
        watches = (await session.execute(select(Watch))).scalars().all()
    wanted_ids = set()
    for watch in watches:
        if watch.enabled:
            wanted_ids.add(f"watch:{watch.id}")
        schedule_watch(watch)
    for job in scheduler.get_jobs():
        if job.id.startswith("watch:") and job.id not in wanted_ids:
            scheduler.remove_job(job.id)


def schedule_news_jobs() -> None:
    from app.news.service import poll_source_by_name, run_release_soon_scan

    settings = get_settings()
    scheduler = get_scheduler()
    for cfg in load_file_config().news_sources:
        job_id = f"news:{cfg.name}"
        if not cfg.enabled:
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
            continue
        interval = max(300, cfg.interval_seconds or settings.news_poll_interval_seconds)
        scheduler.add_job(
            poll_source_by_name,
            IntervalTrigger(seconds=interval, jitter=int(interval * 0.1)),
            args=[cfg.name],
            id=job_id,
            replace_existing=True,
            name=f"news: {cfg.name}",
        )
        log.info("scheduled news source %r every %ss", cfg.name, interval)
    scheduler.add_job(
        run_release_soon_scan,
        CronTrigger(hour=9, minute=0),  # 09:00 Europe/Berlin
        id="news:release-soon-scan",
        replace_existing=True,
        name="release-soon scan",
    )


async def start_scheduler() -> None:
    scheduler = get_scheduler()
    await sync_watch_jobs()
    schedule_news_jobs()
    if not scheduler.running:
        scheduler.start()
    log.info("scheduler started with %d job(s)", len(scheduler.get_jobs()))


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
