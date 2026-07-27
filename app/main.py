"""FastAPI application: dashboard + API + background scheduler, one process."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app import __version__
from app.config import get_settings
from app.db import get_engine, get_sessionmaker, run_migrations
from app.logging_conf import setup_logging
from app.scheduler import get_scheduler, shutdown_scheduler, start_scheduler
from app.seed import seed_watches

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)
    if settings.dashboard_password == "change-me-please":
        log.warning("DASHBOARD_PASSWORD is still the default — set it in .env before exposing!")
    await asyncio.to_thread(run_migrations)
    async with get_sessionmaker()() as session:
        await seed_watches(session)
    await start_scheduler()
    log.info("tcg-tracker %s up (tz=%s)", __version__, settings.timezone)
    try:
        yield
    finally:
        from app.monitor.fetchers import close_client, shutdown_playwright

        shutdown_scheduler()
        await close_client()
        await shutdown_playwright()
        await get_engine().dispose()


app = FastAPI(title="TCG Tracker", version=__version__, lifespan=lifespan, docs_url=None)

app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "web" / "static"),
    name="static",
)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Unauthenticated health endpoint for Docker/uptime checks."""
    db_ok = True
    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    scheduler = get_scheduler()
    healthy = db_ok and scheduler.running
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "version": __version__,
            "database": "ok" if db_ok else "error",
            "scheduler": "running" if scheduler.running else "stopped",
            "jobs": len(scheduler.get_jobs()) if scheduler.running else 0,
        },
    )


@app.get("/healthz/shops")
async def healthz_shops(request: Request) -> JSONResponse:
    """What the running app currently knows about each shop.

    Diagnostics used to probe the shops themselves, which meant every run sent
    fresh requests and could re-arm the very rate limit it was measuring. This
    reports the live process state instead — no shop is contacted.

    Localhost only: it is read by tools running inside the container, and the
    list of monitored shops is nobody else's business. Requests through the
    reverse proxy arrive with its address, not 127.0.0.1.
    """
    from app.monitor import catalog, fetchers

    client = request.client.host if request.client else None
    if client not in ("127.0.0.1", "::1"):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})

    shops: dict[str, dict] = {}
    for host, entry in catalog._cache.items():
        shops.setdefault(host, {})["catalog"] = {
            "products": len(entry.index) if entry.index else 0,
            # Handles let a caller tell "covered by the catalogue" apart from
            # "shop has one, but this product is past the 250-item cap".
            "handles": sorted(entry.index) if entry.index else [],
            "reason": entry.reason,
            "age_seconds": round(time.monotonic() - entry.at, 1),
        }
    for host, buckets in fetchers.cooldown_state().items():
        shops.setdefault(host, {})["cooldown"] = buckets
    return JSONResponse(content={"shops": shops})


from app.web.routes import router as web_router  # noqa: E402  (import after app creation)

app.include_router(web_router)
