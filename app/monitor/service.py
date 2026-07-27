"""Watch checking: fetch -> parse -> record -> detect transitions -> emit events.

Transition rules
----------------
NEW_LISTING   — the product was never seen listed before (watch.listing_seen is
                False, e.g. the URL 404'd or parsed as not-listed) and now shows
                up listed/buyable. On the very first check of a watch that
                already exists in the shop, we mark it seen *silently* unless
                the watch opts in via notify_on_first_seen.
BACK_IN_STOCK — a listed product transitions OUT_OF_STOCK/UNKNOWN -> IN_STOCK.
                Honors the per-watch cooldown so a flapping shop can't spam.

A failing adapter records the error on the watch/check and never raises.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.events import Event, stock_routes
from app.models import EventType, StockCheck, StockStatus, Watch, utcnow
from app.monitor.base import AdapterError, StockResult
from app.monitor.registry import resolve_adapter
from app.notify.service import dispatch_event

log = logging.getLogger(__name__)


def evaluate_transition(watch: Watch, result: StockResult) -> EventType | None:
    """Pure decision function — which event (if any) does this check trigger?"""
    now = utcnow()
    is_first_check = watch.last_check_at is None
    previously_listed = watch.listing_seen
    in_stock = result.status == StockStatus.IN_STOCK

    if result.listed and not previously_listed:
        if is_first_check:
            # "Auch melden, wenn schon verfügbar" — only ping the very first
            # check when the product is actually in stock (not for an already
            # sold-out product); otherwise record the baseline silently.
            if watch.notify_on_first_seen and in_stock:
                return EventType.NEW_LISTING
            return None
        return EventType.NEW_LISTING  # genuinely appeared later (was unlisted)

    if (
        in_stock
        and previously_listed
        and watch.last_status in (StockStatus.OUT_OF_STOCK, StockStatus.UNKNOWN)
    ):
        if watch.last_notified_at is not None and now - watch.last_notified_at < timedelta(
            seconds=watch.cooldown_seconds
        ):
            log.info("watch %s back in stock but within cooldown, suppressing", watch.id)
            return None
        return EventType.BACK_IN_STOCK

    return None


def _apply_result(watch: Watch, result: StockResult) -> None:
    watch.last_check_at = utcnow()
    watch.last_status = result.status
    watch.last_error = None
    if result.listed:
        watch.listing_seen = True
    if result.price is not None:
        watch.last_price = result.price
        watch.last_currency = result.currency or "EUR"
    if result.title:
        watch.last_title = result.title
    if result.image_url:
        watch.last_image_url = result.image_url
    if result.buy_url:
        watch.last_buy_url = result.buy_url
    if result.status == StockStatus.IN_STOCK:
        watch.last_in_stock_at = utcnow()


def _build_event(watch: Watch, result: StockResult, event_type: EventType) -> Event:
    if result.alert_title:
        title = f"{result.alert_title}: {watch.label}"
    elif event_type == EventType.BACK_IN_STOCK:
        title = f"Back in stock: {watch.label}"
    elif result.status == StockStatus.IN_STOCK:
        title = f"Neu gelistet – verfügbar: {watch.label}"
    else:  # new listing that is not (yet) buyable, e.g. preorder / sold out
        title = f"Neu gelistet – noch nicht verfügbar: {watch.label}"
    retailer = watch.retailer or (watch.last_buy_url or watch.url).split("/")[2]
    message = result.title or ""
    return Event(
        type=event_type,
        game=watch.game,
        title=title,
        message=message,
        url=result.buy_url or watch.url,
        cart_url=result.cart_url,
        image_url=result.image_url or watch.last_image_url,
        price=result.price,
        currency=result.currency,
        retailer=retailer,
        watch_id=watch.id,
        routes=stock_routes(watch.game, list(watch.channels or [])) + [f"watch:{watch.id}"],
        priority=watch.priority,
    )


def evaluate_price_target(watch: Watch, result: StockResult) -> Event | None:
    """PRICE_DROP when in stock at/below the target; rearms once the price
    rises above the target (or the item goes out of stock)."""
    if watch.price_target is None or result.price is None:
        return None
    if result.status == StockStatus.IN_STOCK and result.price <= watch.price_target:
        if watch.price_target_hit:
            return None  # already alerted for this dip
        watch.price_target_hit = True
        return Event(
            type=EventType.PRICE_DROP,
            game=watch.game,
            title=f"💰 Preisalarm: {watch.label}",
            message=(
                f"Jetzt {result.price:.2f} € — Ziel war {watch.price_target:.2f} €."
                + (f" ({result.title})" if result.title else "")
            ),
            url=result.buy_url or watch.url,
            image_url=result.image_url or watch.last_image_url,
            price=result.price,
            currency=result.currency,
            retailer=watch.retailer or None,
            watch_id=watch.id,
            routes=stock_routes(watch.game, list(watch.channels or [])) + [f"watch:{watch.id}"],
            priority=watch.priority,
        )
    watch.price_target_hit = False
    return None


async def attach_cardmarket(watch: Watch, event: Event) -> None:
    """Enrich an alert with a reference price so the shop price can be judged.

    Prefers the live Cardmarket price guide (watch.cardmarket_id); falls back to
    the manually entered reference_price/_url, which is the only path available
    while Cardmarket is not handing out API access.

    Never raises: an outage or missing API key must not swallow a restock ping,
    it just leaves the reference field off.
    """
    if watch.cardmarket_id is not None:
        from app.cardmarket import price_reference

        try:
            reference = await price_reference(watch.cardmarket_id)
        except Exception:
            log.warning("cardmarket lookup failed for watch %s", watch.id, exc_info=True)
            reference = None
        if reference is not None and reference.has_price:
            event.cardmarket_trend = reference.trend
            event.cardmarket_low = reference.low
            event.cardmarket_url = reference.url or watch.reference_url
            event.cardmarket_live = True
            return

    if watch.reference_price is not None:
        event.cardmarket_trend = watch.reference_price
        event.cardmarket_live = False
    if watch.reference_url:
        event.cardmarket_url = watch.reference_url


async def check_watch(session: AsyncSession, watch: Watch) -> StockCheck:
    """Run one check for a watch. Commits. Never raises on adapter errors."""
    adapter = resolve_adapter(watch.url, watch.adapter, dict(watch.detection or {}))
    check = StockCheck(watch_id=watch.id, status=StockStatus.UNKNOWN)
    event: Event | None = None
    # Hand the pooled DB connection back BEFORE the network call. Loading the
    # watch opened a transaction, and a fetch can take 30 s+ (unlocker); a
    # handful of slow watches would otherwise hold every connection in the pool
    # and the dashboard could no longer get one at all (HTTP 500).
    # Safe here: nothing is pending yet, and expire_on_commit=False keeps the
    # already-loaded watch attributes usable.
    await session.commit()
    try:
        page, result = await adapter.check(watch.url)
        check.status = result.status
        check.price = result.price
        check.currency = result.currency
        check.title = result.title
        check.http_status = page.status_code
        check.duration_ms = page.elapsed_ms
        if result.note:
            check.error = None if result.status != StockStatus.UNKNOWN else result.note

        event_type = evaluate_transition(watch, result)
        if event_type is not None:
            event = _build_event(watch, result, event_type)
            # the restock ping already shows the price — arm the target so the
            # very next check doesn't send a second, redundant price alert
            if (
                watch.price_target is not None
                and result.price is not None
                and result.price <= watch.price_target
            ):
                watch.price_target_hit = True
        else:
            event = evaluate_price_target(watch, result)
        if event is not None:
            await attach_cardmarket(watch, event)
        _apply_result(watch, result)
        log.info(
            "checked watch %s [%s] -> %s (%s)",
            watch.id,
            adapter.slug,
            result.status.value,
            result.note or "-",
            extra={"watch_id": watch.id, "adapter": adapter.slug},
        )
    except AdapterError as exc:
        # Keep the type in both places. A bare "int() argument must be a string,
        # a bytes-like object or a real number, not ..." named neither the code
        # that broke nor the value, and cost a whole round of guessing.
        check.error = f"{type(exc).__name__}: {exc}"
        watch.last_check_at = utcnow()
        watch.last_error = check.error
        log.warning("watch %s check failed: %s", watch.id, exc, extra={"watch_id": watch.id})
    except Exception as exc:
        check.error = f"unexpected {type(exc).__name__}: {exc}"
        watch.last_check_at = utcnow()
        watch.last_error = check.error
        log.exception("watch %s check crashed", watch.id, extra={"watch_id": watch.id})

    session.add(check)
    await session.commit()

    if event is not None:
        watch.last_notified_at = utcnow()
        await session.commit()
        await dispatch_event(session, event)
    return check


async def check_watch_by_id(watch_id: int) -> None:
    """Scheduler entry point — own session per job run, bounded concurrency."""
    from app.db import get_sessionmaker
    from app.limits import job_slots

    async with job_slots():
        async with get_sessionmaker()() as session:
            watch = await session.get(Watch, watch_id)
            if watch is None or not watch.enabled:
                return
            await check_watch(session, watch)
