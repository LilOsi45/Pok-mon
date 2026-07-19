"""Dashboard routes: login, watches CRUD, news feed, notification history, settings."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import COOKIE_NAME, create_session_cookie, require_auth, verify_password
from app.config import get_settings, load_file_config
from app.db import get_session
from app.events import Event, news_routes, stock_routes
from app.models import (
    DiscoveredSite,
    DiscoveryHunt,
    EventType,
    Game,
    Notification,
    ProductScan,
    ScanItem,
    SetNews,
    StockCheck,
    Watch,
)
from app.monitor.registry import ADAPTER_CLASSES
from app.notify.service import dispatch_event, load_notifiers
from app.scheduler import (
    remove_hunt_job,
    remove_scan_job,
    remove_watch_job,
    schedule_hunt,
    schedule_scan,
    schedule_watch,
)
from app.web.templating import templates

log = logging.getLogger(__name__)

router = APIRouter()
protected = APIRouter(dependencies=[Depends(require_auth)])


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/watches", error: str = ""):
    return templates.TemplateResponse(
        request, "login.html", {"next": next, "error": error, "hide_nav": True}
    )


@router.post("/login")
async def login_submit(request: Request, password: str = Form(...), next: str = Form("/watches")):
    if not await verify_password(password):
        log.warning("failed login attempt from %s", request.client.host if request.client else "?")
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next, "error": "Wrong password.", "hide_nav": True},
            status_code=401,
        )
    if not next.startswith("/") or next.startswith("//"):
        next = "/watches"
    response = RedirectResponse(next, status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        create_session_cookie(),
        max_age=get_settings().session_max_age_seconds,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


# ---------------------------------------------------------------------------
# watches
# ---------------------------------------------------------------------------


async def _watch_rows(session: AsyncSession) -> list[Watch]:
    return list(
        (await session.execute(select(Watch).order_by(Watch.game, Watch.label))).scalars().all()
    )


def _watch_form_context(watch: Watch | None = None) -> dict:
    return {
        "watch": watch,
        "games": [
            (g.value, "Pokémon TCG" if g == Game.POKEMON else "One Piece Card Game") for g in Game
        ],
        "adapters": [(cls.slug, cls.name) for cls in ADAPTER_CLASSES],
        "defaults": {
            "interval": get_settings().default_poll_interval_seconds,
            "cooldown": get_settings().default_cooldown_seconds,
        },
    }


@protected.get("/", response_class=HTMLResponse)
async def index():
    return RedirectResponse("/watches", status_code=303)


@protected.get("/watches", response_class=HTMLResponse)
async def watches_page(request: Request, session: AsyncSession = Depends(get_session)):
    watches = await _watch_rows(session)
    return templates.TemplateResponse(
        request,
        "watches.html",
        {"active": "watches", "watches": watches, **_watch_form_context()},
    )


@protected.get("/watches/table", response_class=HTMLResponse)
async def watches_table(request: Request, session: AsyncSession = Depends(get_session)):
    """HTMX polling target — just the table body."""
    watches = await _watch_rows(session)
    return templates.TemplateResponse(request, "_watch_rows.html", {"watches": watches})


def _apply_watch_form(
    watch: Watch,
    game: str,
    label: str,
    url: str,
    retailer: str,
    adapter: str,
    interval_seconds: int,
    cooldown_seconds: int,
    channels: str,
    price_target: str = "",
    priority: str = "",
) -> None:
    from app.monitor.detection import parse_german_price

    watch.game = Game(game)
    watch.label = label.strip()
    watch.url = url.strip()
    watch.retailer = retailer.strip()
    watch.adapter = adapter or None
    watch.interval_seconds = max(60, interval_seconds)
    watch.cooldown_seconds = max(0, cooldown_seconds)
    watch.channels = [c.strip() for c in channels.split(",") if c.strip()] or [game]
    new_target = parse_german_price(price_target) if price_target.strip() else None
    if new_target != watch.price_target:
        watch.price_target_hit = False  # rearm on target change
    watch.price_target = new_target
    watch.priority = priority == "on"


@protected.post("/watches")
async def create_watch(
    request: Request,
    session: AsyncSession = Depends(get_session),
    game: str = Form(...),
    label: str = Form(...),
    url: str = Form(...),
    retailer: str = Form(""),
    adapter: str = Form(""),
    interval_seconds: int = Form(300),
    cooldown_seconds: int = Form(1800),
    channels: str = Form(""),
    price_target: str = Form(""),
    priority: str = Form(""),
):
    watch = Watch()
    _apply_watch_form(
        watch,
        game,
        label,
        url,
        retailer,
        adapter,
        interval_seconds,
        cooldown_seconds,
        channels,
        price_target,
        priority,
    )
    session.add(watch)
    await session.commit()
    schedule_watch(watch)
    return RedirectResponse("/watches", status_code=303)


@protected.get("/watches/{watch_id}/edit", response_class=HTMLResponse)
async def edit_watch_form(
    request: Request, watch_id: int, session: AsyncSession = Depends(get_session)
):
    watch = await session.get(Watch, watch_id)
    if watch is None:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "_watch_form.html", _watch_form_context(watch))


@protected.post("/watches/{watch_id}")
async def update_watch(
    request: Request,
    watch_id: int,
    session: AsyncSession = Depends(get_session),
    game: str = Form(...),
    label: str = Form(...),
    url: str = Form(...),
    retailer: str = Form(""),
    adapter: str = Form(""),
    interval_seconds: int = Form(300),
    cooldown_seconds: int = Form(1800),
    channels: str = Form(""),
    price_target: str = Form(""),
    priority: str = Form(""),
):
    watch = await session.get(Watch, watch_id)
    if watch is None:
        return HTMLResponse("Not found", status_code=404)
    _apply_watch_form(
        watch,
        game,
        label,
        url,
        retailer,
        adapter,
        interval_seconds,
        cooldown_seconds,
        channels,
        price_target,
        priority,
    )
    await session.commit()
    schedule_watch(watch)
    return RedirectResponse("/watches", status_code=303)


@protected.post("/watches/{watch_id}/toggle", response_class=HTMLResponse)
async def toggle_watch(
    request: Request, watch_id: int, session: AsyncSession = Depends(get_session)
):
    watch = await session.get(Watch, watch_id)
    if watch is None:
        return HTMLResponse("Not found", status_code=404)
    watch.enabled = not watch.enabled
    await session.commit()
    schedule_watch(watch)
    watches = await _watch_rows(session)
    return templates.TemplateResponse(request, "_watch_rows.html", {"watches": watches})


@protected.post("/watches/{watch_id}/check", response_class=HTMLResponse)
async def check_watch_now(
    request: Request, watch_id: int, session: AsyncSession = Depends(get_session)
):
    from app.monitor.service import check_watch

    watch = await session.get(Watch, watch_id)
    if watch is None:
        return HTMLResponse("Not found", status_code=404)
    await check_watch(session, watch)
    watches = await _watch_rows(session)
    return templates.TemplateResponse(request, "_watch_rows.html", {"watches": watches})


@protected.post("/watches/{watch_id}/delete", response_class=HTMLResponse)
async def delete_watch(
    request: Request, watch_id: int, session: AsyncSession = Depends(get_session)
):
    watch = await session.get(Watch, watch_id)
    if watch is not None:
        await session.delete(watch)
        await session.commit()
        remove_watch_job(watch_id)
    watches = await _watch_rows(session)
    return templates.TemplateResponse(request, "_watch_rows.html", {"watches": watches})


# ---------------------------------------------------------------------------
# news / notifications / settings
# ---------------------------------------------------------------------------


@protected.get("/news", response_class=HTMLResponse)
async def news_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    game: str = "",
    page: int = 1,
):
    page_size = 25
    query = select(SetNews).order_by(desc(SetNews.first_seen))
    if game in (g.value for g in Game):
        query = query.where(SetNews.game == Game(game))
    rows = (
        (await session.execute(query.offset((max(1, page) - 1) * page_size).limit(page_size)))
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "news.html",
        {"active": "news", "news": rows, "game": game, "page": max(1, page)},
    )


@protected.get("/watches/{watch_id}/history", response_class=HTMLResponse)
async def watch_history(
    request: Request, watch_id: int, session: AsyncSession = Depends(get_session)
):
    """Price history sparkline (inline SVG) + recent checks for one watch."""
    watch = await session.get(Watch, watch_id)
    if watch is None:
        return HTMLResponse("Not found", status_code=404)
    checks = (
        (
            await session.execute(
                select(StockCheck)
                .where(StockCheck.watch_id == watch_id)
                .order_by(desc(StockCheck.checked_at))
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    checks = list(reversed(checks))  # oldest -> newest
    priced = [c for c in checks if c.price is not None]

    points = ""
    min_price = max_price = None
    if len(priced) >= 2:
        prices = [c.price for c in priced]
        min_price, max_price = min(prices), max(prices)
        span = (max_price - min_price) or 1.0
        width, height, pad = 600.0, 110.0, 8.0
        step = (width - 2 * pad) / (len(priced) - 1)
        points = " ".join(
            f"{pad + i * step:.1f},{height - pad - (p - min_price) / span * (height - 2 * pad):.1f}"
            for i, p in enumerate(prices)
        )
    return templates.TemplateResponse(
        request,
        "_watch_history.html",
        {
            "watch": watch,
            "points": points,
            "min_price": min_price,
            "max_price": max_price,
            "priced_count": len(priced),
            "recent": list(reversed(checks))[:20],  # newest first for the table
        },
    )


# ---------------------------------------------------------------------------
# keyword scanner
# ---------------------------------------------------------------------------


async def _scan_rows(session: AsyncSession) -> list[ProductScan]:
    return list(
        (await session.execute(select(ProductScan).order_by(ProductScan.label))).scalars().all()
    )


async def _scan_context(session: AsyncSession, scan: ProductScan | None = None) -> dict:
    hits = (
        (await session.execute(select(ScanItem).order_by(desc(ScanItem.first_seen)).limit(15)))
        .scalars()
        .all()
    )
    return {
        "scan": scan,
        "scans": await _scan_rows(session),
        "hits": hits,
        "games": [
            (g.value, "Pokémon TCG" if g == Game.POKEMON else "One Piece Card Game") for g in Game
        ],
    }


def _apply_scan_form(
    scan: ProductScan,
    game: str,
    label: str,
    url: str,
    keywords: str,
    exclude_keywords: str,
    interval_seconds: int,
    channels: str,
    use_playwright: str,
    priority: str = "",
) -> None:
    scan.game = Game(game)
    scan.label = label.strip()
    scan.url = url.strip()
    scan.keywords = [k.strip() for k in keywords.split(",") if k.strip()]
    scan.exclude_keywords = [k.strip() for k in exclude_keywords.split(",") if k.strip()]
    scan.interval_seconds = max(300, interval_seconds)
    scan.channels = [c.strip() for c in channels.split(",") if c.strip()] or [game]
    scan.use_playwright = use_playwright == "on"
    scan.priority = priority == "on"


@protected.get("/scanner", response_class=HTMLResponse)
async def scanner_page(request: Request, session: AsyncSession = Depends(get_session)):
    context = await _scan_context(session)
    return templates.TemplateResponse(request, "scanner.html", {"active": "scanner", **context})


@protected.post("/scanner")
async def create_scan(
    request: Request,
    session: AsyncSession = Depends(get_session),
    game: str = Form(...),
    label: str = Form(...),
    url: str = Form(...),
    keywords: str = Form(...),
    exclude_keywords: str = Form(""),
    interval_seconds: int = Form(900),
    channels: str = Form(""),
    use_playwright: str = Form(""),
    priority: str = Form(""),
):
    scan = ProductScan()
    _apply_scan_form(
        scan,
        game,
        label,
        url,
        keywords,
        exclude_keywords,
        interval_seconds,
        channels,
        use_playwright,
        priority,
    )
    session.add(scan)
    await session.commit()
    schedule_scan(scan)
    return RedirectResponse("/scanner", status_code=303)


@protected.get("/scanner/{scan_id}/edit", response_class=HTMLResponse)
async def edit_scan_form(
    request: Request, scan_id: int, session: AsyncSession = Depends(get_session)
):
    scan = await session.get(ProductScan, scan_id)
    if scan is None:
        return HTMLResponse("Not found", status_code=404)
    context = await _scan_context(session, scan)
    return templates.TemplateResponse(request, "_scan_form.html", context)


@protected.post("/scanner/{scan_id}")
async def update_scan(
    request: Request,
    scan_id: int,
    session: AsyncSession = Depends(get_session),
    game: str = Form(...),
    label: str = Form(...),
    url: str = Form(...),
    keywords: str = Form(...),
    exclude_keywords: str = Form(""),
    interval_seconds: int = Form(900),
    channels: str = Form(""),
    use_playwright: str = Form(""),
    priority: str = Form(""),
):
    scan = await session.get(ProductScan, scan_id)
    if scan is None:
        return HTMLResponse("Not found", status_code=404)
    _apply_scan_form(
        scan,
        game,
        label,
        url,
        keywords,
        exclude_keywords,
        interval_seconds,
        channels,
        use_playwright,
        priority,
    )
    await session.commit()
    schedule_scan(scan)
    return RedirectResponse("/scanner", status_code=303)


@protected.post("/scanner/{scan_id}/toggle", response_class=HTMLResponse)
async def toggle_scan(request: Request, scan_id: int, session: AsyncSession = Depends(get_session)):
    scan = await session.get(ProductScan, scan_id)
    if scan is None:
        return HTMLResponse("Not found", status_code=404)
    scan.enabled = not scan.enabled
    await session.commit()
    schedule_scan(scan)
    return templates.TemplateResponse(
        request, "_scan_rows.html", {"scans": await _scan_rows(session)}
    )


@protected.post("/scanner/{scan_id}/check", response_class=HTMLResponse)
async def check_scan_now(
    request: Request, scan_id: int, session: AsyncSession = Depends(get_session)
):
    from app.scanner import run_scan

    scan = await session.get(ProductScan, scan_id)
    if scan is None:
        return HTMLResponse("Not found", status_code=404)
    await run_scan(session, scan)
    return templates.TemplateResponse(
        request, "_scan_rows.html", {"scans": await _scan_rows(session)}
    )


@protected.post("/scanner/{scan_id}/delete", response_class=HTMLResponse)
async def delete_scan(request: Request, scan_id: int, session: AsyncSession = Depends(get_session)):
    scan = await session.get(ProductScan, scan_id)
    if scan is not None:
        await session.delete(scan)
        await session.commit()
        remove_scan_job(scan_id)
    return templates.TemplateResponse(
        request, "_scan_rows.html", {"scans": await _scan_rows(session)}
    )


@protected.get("/scanner/table", response_class=HTMLResponse)
async def scanner_table(request: Request, session: AsyncSession = Depends(get_session)):
    return templates.TemplateResponse(
        request, "_scan_rows.html", {"scans": await _scan_rows(session)}
    )


# ---------------------------------------------------------------------------
# shop discovery
# ---------------------------------------------------------------------------


async def _hunt_rows(session: AsyncSession) -> list[DiscoveryHunt]:
    return list(
        (await session.execute(select(DiscoveryHunt).order_by(DiscoveryHunt.label))).scalars().all()
    )


async def _discovery_context(session: AsyncSession, hunt: DiscoveryHunt | None = None) -> dict:
    sites = (
        (
            await session.execute(
                select(DiscoveredSite).order_by(desc(DiscoveredSite.first_seen)).limit(30)
            )
        )
        .scalars()
        .all()
    )
    return {
        "hunt": hunt,
        "hunts": await _hunt_rows(session),
        "sites": sites,
        "games": [
            (g.value, "Pokémon TCG" if g == Game.POKEMON else "One Piece Card Game") for g in Game
        ],
    }


@protected.get("/discovery", response_class=HTMLResponse)
async def discovery_page(request: Request, session: AsyncSession = Depends(get_session)):
    context = await _discovery_context(session)
    return templates.TemplateResponse(request, "discovery.html", {"active": "discovery", **context})


@protected.get("/discovery/table", response_class=HTMLResponse)
async def discovery_table(request: Request, session: AsyncSession = Depends(get_session)):
    return templates.TemplateResponse(
        request, "_hunt_rows.html", {"hunts": await _hunt_rows(session)}
    )


@protected.post("/discovery")
async def create_hunt(
    request: Request,
    session: AsyncSession = Depends(get_session),
    game: str = Form(...),
    label: str = Form(...),
    query: str = Form(...),
    interval_seconds: int = Form(21600),
    channels: str = Form(""),
    priority: str = Form(""),
):
    hunt = DiscoveryHunt(
        game=Game(game),
        label=label.strip(),
        query=query.strip(),
        interval_seconds=max(3600, interval_seconds),
        channels=[c.strip() for c in channels.split(",") if c.strip()],
        priority=priority == "on",
    )
    session.add(hunt)
    await session.commit()
    schedule_hunt(hunt)
    return RedirectResponse("/discovery", status_code=303)


@protected.post("/discovery/{hunt_id}/toggle", response_class=HTMLResponse)
async def toggle_hunt(request: Request, hunt_id: int, session: AsyncSession = Depends(get_session)):
    hunt = await session.get(DiscoveryHunt, hunt_id)
    if hunt is None:
        return HTMLResponse("Not found", status_code=404)
    hunt.enabled = not hunt.enabled
    await session.commit()
    schedule_hunt(hunt)
    return templates.TemplateResponse(
        request, "_hunt_rows.html", {"hunts": await _hunt_rows(session)}
    )


@protected.post("/discovery/{hunt_id}/run", response_class=HTMLResponse)
async def run_hunt_now(
    request: Request, hunt_id: int, session: AsyncSession = Depends(get_session)
):
    from app.discovery import run_hunt

    hunt = await session.get(DiscoveryHunt, hunt_id)
    if hunt is None:
        return HTMLResponse("Not found", status_code=404)
    await run_hunt(session, hunt)
    return templates.TemplateResponse(
        request, "_hunt_rows.html", {"hunts": await _hunt_rows(session)}
    )


@protected.post("/discovery/{hunt_id}/delete", response_class=HTMLResponse)
async def delete_hunt(request: Request, hunt_id: int, session: AsyncSession = Depends(get_session)):
    hunt = await session.get(DiscoveryHunt, hunt_id)
    if hunt is not None:
        await session.delete(hunt)
        await session.commit()
        remove_hunt_job(hunt_id)
    return templates.TemplateResponse(
        request, "_hunt_rows.html", {"hunts": await _hunt_rows(session)}
    )


@protected.get("/shops", response_class=HTMLResponse)
async def shops_page(request: Request, q: str = ""):
    from dataclasses import asdict

    from app.shops import SHOP_CATALOG, search_url

    shops = []
    for shop in SHOP_CATALOG:
        row = asdict(shop)
        row["search_link"] = search_url(shop, q) if q else ""
        shops.append(row)
    return templates.TemplateResponse(
        request, "shops.html", {"active": "shops", "shops": shops, "q": q.strip()}
    )


@protected.get("/notifications", response_class=HTMLResponse)
async def notifications_page(
    request: Request, session: AsyncSession = Depends(get_session), page: int = 1
):
    page_size = 50
    rows = (
        (
            await session.execute(
                select(Notification)
                .order_by(desc(Notification.created_at))
                .offset((max(1, page) - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {"active": "notifications", "notifications": rows, "page": max(1, page)},
    )


@protected.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request, session: AsyncSession = Depends(get_session), flash: str = ""
):
    settings = get_settings()
    file_config = load_file_config()
    notifiers = await load_notifiers(session)
    checks_count = (await session.execute(select(func.count(StockCheck.id)))).scalar() or 0
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active": "settings",
            "flash": flash,
            "notifiers": notifiers,
            "news_sources": file_config.news_sources,
            "settings_view": {
                "Timezone": settings.timezone,
                "Database": settings.database_url.split("://")[0],
                "Default poll interval": f"{settings.default_poll_interval_seconds}s",
                "Default cooldown": f"{settings.default_cooldown_seconds}s",
                "News poll interval": f"{settings.news_poll_interval_seconds}s",
                "Release-soon lead": f"{settings.release_soon_lead_days} days",
                "Robots.txt": "respected" if settings.respect_robots_txt else "ignored",
                "Proxy": "configured" if settings.proxy_url else "—",
                "Cardmarket API": "configured" if settings.cardmarket_app_token else "—",
                "Stock checks recorded": f"{checks_count}",
            },
        },
    )


@protected.post("/settings/test-notification")
async def test_notification(request: Request, session: AsyncSession = Depends(get_session)):
    event = Event(
        type=EventType.TEST,
        game=Game.POKEMON,
        title="Test notification from TCG Tracker",
        message="If you can read this, webhooks are wired up correctly. ✔",
        url="https://example.com",
        routes=stock_routes(Game.POKEMON) + news_routes(Game.POKEMON) + ["*"],
    )
    records = await dispatch_event(session, event)
    sent = sum(1 for r in records if r.success)
    return RedirectResponse(
        f"/settings?flash=Test+sent+to+{sent}/{len(records)}+notifier(s)", status_code=303
    )


router.include_router(protected)
