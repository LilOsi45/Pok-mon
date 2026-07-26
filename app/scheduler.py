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
from app.models import DiscoveryHunt, ProductScan, Watch

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


def schedule_scan(scan: ProductScan) -> None:
    """Add or replace the polling job for a keyword scanner."""
    from app.scanner import run_scan_by_id

    scheduler = get_scheduler()
    job_id = f"scan:{scan.id}"
    if not scan.enabled:
        remove_scan_job(scan.id)
        return
    interval = max(300, scan.interval_seconds or 900)
    scheduler.add_job(
        run_scan_by_id,
        IntervalTrigger(seconds=interval, jitter=int(interval * 0.2)),
        args=[scan.id],
        id=job_id,
        replace_existing=True,
        name=f"scan {scan.id}: {scan.label}",
    )
    log.info("scheduled scan %s every %ss (±20%% jitter)", scan.id, interval)


def remove_scan_job(scan_id: int) -> None:
    job_id = f"scan:{scan_id}"
    if get_scheduler().get_job(job_id):
        get_scheduler().remove_job(job_id)


async def sync_scan_jobs() -> None:
    from app.db import get_sessionmaker

    scheduler = get_scheduler()
    async with get_sessionmaker()() as session:
        scans = (await session.execute(select(ProductScan))).scalars().all()
    wanted_ids = {f"scan:{s.id}" for s in scans if s.enabled}
    for scan in scans:
        schedule_scan(scan)
    for job in scheduler.get_jobs():
        if job.id.startswith("scan:") and job.id not in wanted_ids:
            scheduler.remove_job(job.id)


def schedule_hunt(hunt: DiscoveryHunt) -> None:
    """Add or replace the job for a shop-discovery hunt."""
    from app.discovery import run_hunt_by_id

    scheduler = get_scheduler()
    job_id = f"hunt:{hunt.id}"
    if not hunt.enabled:
        remove_hunt_job(hunt.id)
        return
    interval = max(3600, hunt.interval_seconds or 21600)  # searches stay rare
    scheduler.add_job(
        run_hunt_by_id,
        IntervalTrigger(seconds=interval, jitter=int(interval * 0.2)),
        args=[hunt.id],
        id=job_id,
        replace_existing=True,
        name=f"hunt {hunt.id}: {hunt.label}",
    )
    log.info("scheduled hunt %s every %ss (±20%% jitter)", hunt.id, interval)


def remove_hunt_job(hunt_id: int) -> None:
    job_id = f"hunt:{hunt_id}"
    if get_scheduler().get_job(job_id):
        get_scheduler().remove_job(job_id)


async def sync_hunt_jobs() -> None:
    from app.db import get_sessionmaker

    scheduler = get_scheduler()
    async with get_sessionmaker()() as session:
        hunts = (await session.execute(select(DiscoveryHunt))).scalars().all()
    wanted_ids = {f"hunt:{h.id}" for h in hunts if h.enabled}
    for hunt in hunts:
        schedule_hunt(hunt)
    for job in scheduler.get_jobs():
        if job.id.startswith("hunt:") and job.id not in wanted_ids:
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
    if settings.heartbeat_enabled:
        from app.heartbeat import send_heartbeat

        scheduler.add_job(
            send_heartbeat,
            CronTrigger(hour=max(0, min(23, settings.heartbeat_hour)), minute=15),
            id="system:heartbeat",
            replace_existing=True,
            name="daily heartbeat",
        )

    # Trim the check/notification history nightly — it grows by tens of
    # thousands of rows a day and nothing else ever deletes from it.
    from app.retention import prune_job

    scheduler.add_job(
        prune_job,
        CronTrigger(hour=4, minute=30),
        id="system:retention",
        replace_existing=True,
        name="prune old history",
    )


async def start_scheduler() -> None:
    scheduler = get_scheduler()
    await sync_watch_jobs()
    await sync_scan_jobs()
    await sync_hunt_jobs()
    schedule_news_jobs()
    if not scheduler.running:
        scheduler.start()
    log.info("scheduler started with %d job(s)", len(scheduler.get_jobs()))


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
