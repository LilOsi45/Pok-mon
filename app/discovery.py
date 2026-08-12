"""Shop discovery: web-search for NEW shops selling a product, vet them, and
post the product link to Discord.

Pipeline per hunt run:
1. Web search for the hunt query (Brave Search API when BRAVE_API_KEY is set,
   otherwise the DuckDuckGo HTML endpoint — keyless but rate-limit sensitive,
   so hunts default to a 6h interval).
2. Drop known territory: catalog shops, marketplaces/socials/price comparers,
   and domains already discovered earlier (global dedupe table).
3. For each genuinely new domain: fetch the product hit (title/price/stock via
   the shared detection pipeline) and vet the shop —
   Impressum/AGB/Datenschutz links (German legal must-haves), buyer-protected
   payment methods (PayPal/Klarna) vs. prepayment-only, and the Trustpilot
   profile (rating + review count from their JSON-LD).
4. Verdict: trusted / check / suspicious. Trusted & check hits get a Discord
   ping with the product link and the transparent check summary; suspicious
   ones are only stored for the dashboard — no scam links pushed to your phone.

The vetting is a heuristic, not a guarantee — the message says so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit

import httpx
from selectolax.parser import HTMLParser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.events import Event
from app.models import DiscoveredSite, DiscoveryHunt, EventType, utcnow
from app.monitor.detection import detect_stock, parse_json_ld
from app.notify.service import dispatch_event
from app.shops import SHOP_CATALOG

log = logging.getLogger(__name__)

MAX_NEW_DOMAINS_PER_RUN = 8  # each new domain costs ~3 fetches
MAX_PINGS_PER_RUN = 5

# Domains that are never "new shop" material: search engines, socials,
# marketplaces we know, price comparers, wikis/news. Token match on domain.
EXCLUDED_TOKENS = (
    "google.",
    "bing.",
    "duckduckgo.",
    "youtube.",
    "reddit.",
    "facebook.",
    "instagram.",
    "tiktok.",
    "twitter.",
    "x.com",
    "pinterest.",
    "wikipedia.",
    "ebay.",
    "kleinanzeigen.",
    "idealo.",
    "geizhals.",
    "billiger.",
    "check24.",
    "amazon.",
    "etsy.",
    "temu.",
    "aliexpress.",
    "wish.com",
    "rakuten.",
    "hood.de",
    "trustpilot.",
    "cardmarket.",
    "pokemon.com",
    "pokewiki.",
    "bulbapedia.",
    "pokebeach.",
    "serebii.",
    "twitch.",
    "discord.",
    "github.",
    "reddit.com",
    "onepiece-cardgame.",
)


@dataclass(slots=True)
class SearchHit:
    url: str
    title: str
    snippet: str = ""


@dataclass(slots=True)
class SiteCheck:
    score: int = 0
    verdict: str = "check"  # trusted | check | suspicious
    checks: dict = field(default_factory=dict)


def normalize_domain(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def is_excluded_domain(domain: str) -> bool:
    if any(token in domain for token in EXCLUDED_TOKENS):
        return True
    for shop in SHOP_CATALOG:  # catalog shops are covered by watches/scanners
        if domain == shop.domain or domain.endswith("." + shop.domain):
            return True
    return False


# ---------------------------------------------------------------------------
# web search
# ---------------------------------------------------------------------------


def parse_ddg_html(html: str) -> list[SearchHit]:
    """Parse the DuckDuckGo HTML endpoint's organic results."""
    tree = HTMLParser(html)
    hits: list[SearchHit] = []
    for result in tree.css("div.result"):
        anchor = result.css_first("a.result__a")
        if anchor is None:
            continue
        href = (anchor.attributes or {}).get("href") or ""
        # DDG wraps targets: //duckduckgo.com/l/?uddg=<encoded>&rut=...
        if "duckduckgo.com/l/" in href:
            qs = parse_qs(urlsplit(href).query)
            href = unquote(qs.get("uddg", [""])[0])
        if not href.startswith("http"):
            continue
        snippet_node = result.css_first(".result__snippet")
        hits.append(
            SearchHit(
                url=href,
                title=anchor.text(deep=True, strip=True),
                snippet=snippet_node.text(deep=True, strip=True) if snippet_node else "",
            )
        )
    return hits


async def search_web(query: str, limit: int = 20) -> list[SearchHit]:
    settings = get_settings()
    if settings.brave_api_key:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": limit, "country": "de"},
                headers={"X-Subscription-Token": settings.brave_api_key},
            )
        resp.raise_for_status()
        results = (resp.json().get("web") or {}).get("results") or []
        return [
            SearchHit(
                url=r.get("url", ""), title=r.get("title", ""), snippet=r.get("description", "")
            )
            for r in results
            if r.get("url")
        ][:limit]

    # Keyless fallback: DuckDuckGo HTML endpoint. DDG blocks datacenter/proxy
    # IPs (HTTP 202), so route it through the scraping API when available;
    # otherwise a plain fetch (works only from a clean IP). Low frequency
    # (default 6h interval) keeps the cost negligible.
    from app.monitor import fetchers

    ddg_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    if settings.scraper_api_key:
        page = await fetchers.fetch_scraperapi(ddg_url)
    else:
        page = await fetchers.fetch_httpx(
            ddg_url,
            respect_robots=False,
            extra_headers={"Referer": "https://duckduckgo.com/"},
        )
    if page.status_code != 200:
        raise RuntimeError(f"search returned HTTP {page.status_code}")
    return parse_ddg_html(page.text)[:limit]


# ---------------------------------------------------------------------------
# vetting
# ---------------------------------------------------------------------------


def _score_homepage(html: str) -> tuple[int, dict]:
    tree = HTMLParser(html)
    text_lower = html.lower()
    checks: dict = {}
    score = 0

    def has_legal_link(marker: str) -> bool:
        for a in tree.css("a[href]"):
            attrs = a.attributes or {}
            blob = f"{attrs.get('href', '')} {a.text(deep=True, strip=True)}".lower()
            if marker in blob:
                return True
        return False

    checks["impressum"] = has_legal_link("impressum")
    score += 3 if checks["impressum"] else -3
    checks["agb"] = has_legal_link("agb") or "widerruf" in text_lower
    score += 1 if checks["agb"] else 0
    checks["datenschutz"] = has_legal_link("datenschutz") or "privacy" in text_lower
    score += 1 if checks["datenschutz"] else 0

    buyer_protected = any(m in text_lower for m in ("paypal", "klarna", "rechnungskauf"))
    prepay_only = ("vorkasse" in text_lower or "überweisung" in text_lower) and not buyer_protected
    checks["payment"] = (
        "paypal/klarna" if buyer_protected else ("nur vorkasse?" if prepay_only else "unklar")
    )
    score += 2 if buyer_protected else (-2 if prepay_only else 0)
    return score, checks


def parse_trustpilot(html: str) -> tuple[float | None, int | None]:
    """Extract (rating, review_count) from a Trustpilot review page's JSON-LD."""
    import json as _json
    import re as _re

    for m in _re.finditer(r'"aggregateRating"\s*:\s*{[^}]*}', html[:400000], flags=_re.DOTALL):
        try:
            block = _json.loads("{" + m.group(0) + "}")["aggregateRating"]
            rating = float(block.get("ratingValue"))
            count = int(block.get("reviewCount") or block.get("ratingCount") or 0)
            return rating, count
        except (ValueError, TypeError, KeyError, _json.JSONDecodeError):
            continue
    return None, None


async def vet_site(domain: str) -> SiteCheck:
    from app.monitor import fetchers

    check = SiteCheck()
    try:
        # one-time vetting fetch of the storefront, not recurring scraping
        home = await fetchers.fetch_httpx(f"https://{domain}/", respect_robots=False, retries=2)
        if home.status_code != 200:
            check.checks["homepage"] = f"HTTP {home.status_code}"
            check.score -= 3
        else:
            score, checks = _score_homepage(home.text)
            check.score += score
            check.checks.update(checks)
    except Exception as exc:
        check.checks["homepage"] = f"nicht erreichbar ({str(exc)[:60]})"
        check.score -= 4

    try:
        async with httpx.AsyncClient(
            timeout=15,
            headers={"User-Agent": get_settings().user_agent},
            follow_redirects=True,
        ) as client:
            resp = await client.get(f"https://de.trustpilot.com/review/{domain}")
        if resp.status_code == 200:
            rating, count = parse_trustpilot(resp.text)
            if rating is not None:
                check.checks["trustpilot_rating"] = rating
                check.checks["trustpilot_reviews"] = count
                if (count or 0) >= 50 and rating >= 3.7:
                    check.score += 4
                elif rating < 3.0:
                    check.score -= 3
                else:
                    check.score += 1
        else:
            check.checks["trustpilot"] = "kein Profil"
    except httpx.HTTPError:
        check.checks["trustpilot"] = "nicht prüfbar"

    check.verdict = (
        "trusted" if check.score >= 6 else ("check" if check.score >= 2 else "suspicious")
    )
    return check


# ---------------------------------------------------------------------------
# hunt execution
# ---------------------------------------------------------------------------

VERDICT_LABEL = {
    "trusted": "✅ wirkt seriös",
    "check": "🟡 selbst prüfen",
    "suspicious": "🔴 verdächtig",
}


def _checks_line(checks: dict) -> str:
    parts: list[str] = []
    for key, label in (("impressum", "Impressum"), ("agb", "AGB"), ("datenschutz", "Datenschutz")):
        if key in checks:
            parts.append(f"{label} {'✓' if checks[key] else '✗'}")
    if payment := checks.get("payment"):
        parts.append(f"Zahlung: {payment}")
    if (rating := checks.get("trustpilot_rating")) is not None:
        parts.append(f"Trustpilot {rating}★ ({checks.get('trustpilot_reviews', 0)} Bew.)")
    elif tp := checks.get("trustpilot"):
        parts.append(f"Trustpilot: {tp}")
    return " · ".join(parts)


def _site_event(hunt: DiscoveryHunt, site: DiscoveredSite) -> Event:
    lines = [VERDICT_LABEL.get(site.verdict, site.verdict)]
    if site.title:
        price = f" — {site.price:.2f} €" if site.price else ""
        lines.append(f"**{site.title[:150]}**{price}")
    if summary := _checks_line(dict(site.checks or {})):
        lines.append(summary)
    lines.append(f"Gefunden über Suche: „{hunt.query}“ — Check ist eine Heuristik, keine Garantie.")
    return Event(
        type=EventType.NEW_SHOP_FOUND,
        game=site.game,
        title=f"🔎 Neuer Shop gefunden: {site.domain}",
        message="\n".join(lines)[:3900],
        url=site.url,
        price=site.price,
        retailer=site.domain,
        routes=[f"discovery:{site.game.value}"]
        + [c if ":" in c else f"channel:{c}" for c in (hunt.channels or [])],
        priority=hunt.priority,
    )


async def run_hunt(session: AsyncSession, hunt: DiscoveryHunt) -> list[DiscoveredSite]:
    """Run one discovery pass. Commits. Never raises on search/fetch errors."""
    from app.monitor import fetchers

    try:
        hits = await search_web(hunt.query)
    except Exception as exc:
        hunt.last_run_at = utcnow()
        hunt.last_error = str(exc)[:500]
        await session.commit()
        log.warning("hunt %s search failed: %s", hunt.id, exc)
        return []

    known = set((await session.execute(select(DiscoveredSite.domain))).scalars())
    created: list[DiscoveredSite] = []
    seen_this_run: set[str] = set()
    for hit in hits:
        if len(created) >= MAX_NEW_DOMAINS_PER_RUN:
            break
        domain = normalize_domain(hit.url)
        if not domain or domain in known or domain in seen_this_run:
            continue
        if is_excluded_domain(domain):
            continue
        seen_this_run.add(domain)

        title, price = hit.title, None
        try:
            product_page = await fetchers.fetch_httpx(hit.url, respect_robots=False, retries=2)
            if product_page.status_code == 200:
                stock = parse_json_ld(product_page.text) or detect_stock(product_page.text)
                title = stock.title or title
                price = stock.price
        except Exception as exc:
            log.info("hunt %s: product fetch failed for %s: %s", hunt.id, hit.url, exc)

        vetted = await vet_site(domain)
        created.append(
            DiscoveredSite(
                domain=domain,
                game=hunt.game,
                hunt_id=hunt.id,
                url=hit.url,
                title=(title or "")[:300] or None,
                price=price,
                verdict=vetted.verdict,
                score=vetted.score,
                checks=vetted.checks,
            )
        )

    hunt.last_run_at = utcnow()
    hunt.last_error = None
    session.add_all(created)
    await session.commit()
    log.info(
        "hunt %s (%s): %d results, %d new domain(s)",
        hunt.id,
        hunt.label,
        len(hits),
        len(created),
    )

    pinged = 0
    for site in created:
        # suspicious shops stay dashboard-only — no scam links pushed to Discord
        if site.verdict == "suspicious" or pinged >= MAX_PINGS_PER_RUN:
            continue
        await dispatch_event(session, _site_event(hunt, site))
        site.notified = True
        pinged += 1
    await session.commit()
    return created


async def run_hunt_by_id(hunt_id: int) -> None:
    """Scheduler entry point."""
    from app.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        hunt = await session.get(DiscoveryHunt, hunt_id)
        if hunt is None or not hunt.enabled:
            return
        await run_hunt(session, hunt)
