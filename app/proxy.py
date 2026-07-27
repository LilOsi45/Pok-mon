"""Send only what has to go through the paid proxy.

Residential proxies bill per gigabyte, so the question is never "proxy or not"
but "which requests". Measured on this install, three numbers decide the bill:

  * 12 of 15 shops answer the server's own IP perfectly well. Routing those
    through the proxy would multiply the bill for nothing.
  * Ordinary pages are not what gets refused — /products.json is. Sending only
    that endpoint through the proxy leaves most traffic free.
  * One catalogue request serves every watch on its shop. Proxying the
    catalogue instead of 18 product pages is an 18x saving on the same data.

So nothing is proxied by default. A shop/bucket is escalated only after it has
actually been refused, and a daily budget stops the bill from running away if a
shop starts refusing everything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from app.config import get_settings

log = logging.getLogger(__name__)

Key = tuple[str, str]  # (domain, bucket)


@dataclass
class Usage:
    requests: int = 0
    bytes: int = 0

    def add(self, size: int) -> None:
        self.requests += 1
        self.bytes += max(size, 0)


@dataclass
class _State:
    day: date = field(default_factory=date.today)
    usage: dict[Key, Usage] = field(default_factory=dict)
    escalated: set[Key] = field(default_factory=set)
    refusals: dict[Key, int] = field(default_factory=dict)

    def roll_over(self) -> None:
        today = date.today()
        if today != self.day:
            self.day = today
            self.usage.clear()


_state = _State()


def reset() -> None:
    global _state
    _state = _State()


def normalize_proxy_url(raw: str | None) -> str | None:
    """Accept whatever shape the provider hands out, or nothing at all.

    Providers list credentials as `host:port:user:pass`, which is not a URL.
    Rearranging those four fields by hand is easy to get wrong and fails in a
    way that is hard to read: a misplaced field put a session token where the
    port belongs, urlsplit raised "Port could not be cast to integer", and
    *every* fetch failed — 41 of 43 watches in error with no obvious link to
    the proxy.

    So the field order is worked out here instead: the numeric field is the
    port, the one with dots is the host, and the remaining two are user and
    password in that order.
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()

    scheme = "http"
    if "://" in text:
        scheme, _, text = text.partition("://")

    # An "@" already says which half is which. Never reorder in that case — a
    # password containing a dot would otherwise be mistaken for the host.
    if "@" in text:
        credentials, _, hostport = text.rpartition("@")
        host, _, port = hostport.rpartition(":")
        if not host or not port.isdigit():
            log.error("PROXY_URL has no numeric port — proxy disabled")
            return None
        return f"{scheme}://{credentials}@{host}:{port}"

    parts = [p for p in text.split(":") if p]
    if len(parts) == 2 and parts[1].isdigit():  # host:port, no credentials
        return f"{scheme}://{parts[0]}:{parts[1]}"
    if len(parts) != 4:
        log.error("PROXY_URL not understood (%d parts) — proxy disabled", len(parts))
        return None

    # Four bare fields, order unknown: providers print host:port:user:pass, but
    # getting that wrong by hand is what put a session token where the port
    # belongs and broke every request. Identify the fields instead of trusting
    # a position.
    # Positions, not values: a password that happens to read like the port
    # would otherwise be dropped and the unpacking below would raise.
    port_at = next((i for i, p in enumerate(parts) if p.isdigit() and len(p) <= 5), None)
    host_at = next((i for i, p in enumerate(parts) if "." in p and not p.isdigit()), None)
    if port_at is None or host_at is None or port_at == host_at:
        log.error("PROXY_URL has no recognisable host/port — proxy disabled")
        return None
    rest = [p for i, p in enumerate(parts) if i not in (port_at, host_at)]
    user, password = rest
    return f"{scheme}://{user}:{password}@{parts[host_at]}:{parts[port_at]}"


def url() -> str | None:
    return normalize_proxy_url(get_settings().proxy_url)


def configured() -> bool:
    return bool(url())


def _always(domain: str) -> bool:
    """Shops the operator pinned to the proxy in .env."""
    listed = get_settings().proxy_shops.replace(" ", "")
    if not listed:
        return False
    wanted = {host.lower().removeprefix("www.") for host in listed.split(",") if host}
    return domain in wanted


def totals() -> Usage:
    _state.roll_over()
    total = Usage()
    for used in _state.usage.values():
        total.requests += used.requests
        total.bytes += used.bytes
    return total


def budget_left() -> tuple[int, float]:
    """Remaining requests and megabytes for today."""
    settings = get_settings()
    used = totals()
    return (
        max(settings.proxy_daily_request_budget - used.requests, 0),
        max(settings.proxy_daily_mb_budget - used.bytes / 1_000_000, 0.0),
    )


def budget_exhausted() -> bool:
    requests_left, mb_left = budget_left()
    return requests_left <= 0 or mb_left <= 0


def routes_via_proxy(domain: str, bucket: str) -> bool:
    if not configured():
        return False
    wanted = (
        get_settings().proxy_mode == "always"
        or _always(domain)
        or (domain, bucket) in _state.escalated
    )
    if not wanted:
        return False
    if budget_exhausted():
        # Falling back to the direct route keeps checks running rather than
        # failing outright — a spent budget must not become an outage.
        log.warning("proxy budget for today is used up — %s/%s stays direct", domain, bucket)
        return False
    return True


def note_refusal(domain: str, bucket: str, *, was_proxied: bool) -> bool:
    """Count a refusal; returns True when this bucket just moved to the proxy.

    A refusal that already went through the proxy is not an argument for the
    proxy — escalating on it would only burn budget on a shop that dislikes us
    either way.
    """
    if was_proxied or not configured():
        return False
    key = (domain, bucket)
    if key in _state.escalated:
        return False
    _state.refusals[key] = _state.refusals.get(key, 0) + 1
    if _state.refusals[key] < get_settings().proxy_after_refusals:
        return False
    _state.escalated.add(key)
    log.warning(
        "routing %s/%s through the proxy after %d refusals", domain, bucket, _state.refusals[key]
    )
    return True


def note_success(domain: str, bucket: str, size: int, *, was_proxied: bool) -> None:
    key = (domain, bucket)
    if was_proxied:
        _state.roll_over()
        _state.usage.setdefault(key, Usage()).add(size)
        return
    # It works without the proxy again — stop paying for it.
    _state.refusals.pop(key, None)
    _state.escalated.discard(key)


def report() -> dict:
    _state.roll_over()
    settings = get_settings()
    used = totals()
    per_shop = {
        f"{domain}/{bucket}": {"requests": u.requests, "megabytes": round(u.bytes / 1_000_000, 2)}
        for (domain, bucket), u in sorted(_state.usage.items())
    }
    return {
        "configured": configured(),
        "mode": settings.proxy_mode,
        "today": {
            "requests": used.requests,
            "megabytes": round(used.bytes / 1_000_000, 2),
            "estimated_cost": round(used.bytes / 1_000_000_000 * settings.proxy_cost_per_gb, 2),
        },
        "budget": {
            "requests_left": budget_left()[0],
            "megabytes_left": round(budget_left()[1], 1),
        },
        "via_proxy": sorted(f"{d}/{b}" for d, b in _state.escalated),
        "per_shop": per_shop,
    }
