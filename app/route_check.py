"""Would an alarm actually arrive? Match every watch and scanner to a notifier.

    docker compose exec -T app python -m app.route_check

A watch can work perfectly and still be useless: if no notifier listens on its
channels, the ping is built, found to be unroutable, and nothing reaches the
phone. That is not hypothetical — the daily heartbeat that would have reported
the broken Pokémon Center scanner matched no notifier and vanished, which is why
a drop went unnoticed. Everything else in this project measures whether we can
*see* a drop; this measures whether we can *say* it.

Reads only the database and config.yaml — it contacts no shop and sends nothing.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit

from sqlalchemy import select

from app.config import get_settings
from app.events import Event, stock_routes
from app.models import EventType, ProductScan, Watch
from app.notify.base import Notifier
from app.watchdog import ALARM_ROUTES

# System events carry no game and fixed routes. Imported rather than copied:
# a hand-kept duplicate of the watchdog's routes went stale the moment they
# changed, and this report exists precisely to be trusted about routing.
SYSTEM_SENDERS: tuple[tuple[str, list[str]], ...] = (
    ("Tagesbericht (heartbeat)", ["system:heartbeat"]),
    ("Störungs-Alarm (watchdog)", list(ALARM_ROUTES)),
)
NEWS_SENDERS: tuple[tuple[str, list[str]], ...] = (
    ("Pokémon-News", ["news:pokemon"]),
    ("One-Piece-News", ["news:one_piece"]),
)
LOST = "NIEMAND — Alarm geht verloren"
UNSEEN = "NIEMAND — Störungen bleiben unbemerkt"


def receivers(notifiers: list[Notifier], routes: list[str]) -> list[str]:
    """Which notifiers would take an event with these routes.

    Deliberately not EventType.TEST: `matches` returns True for test events on
    every notifier, so probing with one would report every gap as fine.
    """
    probe = Event(type=EventType.BACK_IN_STOCK, game=None, title="Routen-Prüfung", routes=routes)
    return [n.name for n in notifiers if n.matches(probe)]


def avatar_problem(url: str) -> str | None:
    """Why Discord would show no picture for this URL.

    The logo vanished from every ping at once and nothing in the sending code
    had changed, so the URL was the suspect. Discord fetches it from its own
    servers, which rules out more than it looks like:

      * plain http is dropped without comment — Discord requires https;
      * a bare IP with an application port is usually only reachable inside the
        network or behind the reverse proxy, never from Discord;
      * its own attachment links are signed and expire (?ex=…&hm=…), so an
        avatar that worked for days silently starts 404ing.

    All three fail the same way: no error anywhere, just no picture.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        return (
            "Discord lädt das Bild selbst und akzeptiert nur https — bei http\n"
            "  wird es kommentarlos weggelassen. Genau das passiert hier."
        )
    if re.fullmatch(r"[\d.]+(:\d+)?", parts.netloc):
        return (
            "Das ist eine nackte IP-Adresse. Discord muss die aus dem Internet\n"
            "  erreichen können — nimm die Adresse, unter der du das Dashboard im\n"
            "  Browser öffnest."
        )
    if parts.port not in (None, 443):
        return (
            f"Port {parts.port} ist nicht der normale https-Port. Von außen ist der\n"
            "  meist zu — nimm die Adresse, unter der du das Dashboard öffnest."
        )
    if "cdn.discordapp.com" in parts.netloc and ("ex=" in parts.query or "hm=" in parts.query):
        return (
            "Das ist ein Discord-Anhang-Link. Die laufen nach kurzer Zeit ab und\n"
            "  liefern dann nichts mehr — das Bild woanders dauerhaft ablegen."
        )
    return None


def avatar_status() -> str:
    """The picture the pings carry, and what is wrong with it."""
    url = (get_settings().discord_avatar_url or "").strip()
    if not url:
        return (
            "Bild der Pings: keines eingestellt — Discord zeigt das Bild des Webhooks.\n"
            "  Eigenes Logo: DISCORD_AVATAR_URL in der .env auf eine dauerhafte Bild-URL setzen."
        )
    line = f"Bild der Pings: {url[:90]}"
    if problem := avatar_problem(url):
        line += f"\n  ACHTUNG: {problem}"
    return line


def render(notifiers: list[Notifier], watches: list[Watch], scans: list[ProductScan]) -> str:
    if not notifiers:
        return "Es ist kein Notifier eingerichtet — kein Alarm kann ankommen.\n"

    out: list[str] = [avatar_status(), "", "Eingerichtete Kanäle:"]
    for n in notifiers:
        out.append(f"  {n.name:24} hört auf: {', '.join(n.routes) or '(nichts)'}")

    orphans: list[str] = []
    header = f"{'':>4}  {'Name':30}  Empfänger"

    for title, kind, rows in (("WATCHES", "Watch", watches), ("SCANNER", "Scanner", scans)):
        out += ["", title, header]
        listed = False
        for row in rows:
            if not row.enabled:
                continue
            listed = True
            routes = stock_routes(row.game, list(row.channels or []))
            got = receivers(notifiers, routes)
            out.append(f"{row.id:>4}  {row.label[:30]:30}  {', '.join(got) or LOST}")
            if not got:
                orphans.append(f"{kind} {row.id} ({row.label}): {', '.join(routes)}")
        if not listed:
            out.append("      (keine aktiven)")

    # News and system reports use their own route families, so a stock-only
    # notifier can look complete above while every one of these goes nowhere.
    out += ["", "SYSTEM & NEWS"]
    for label, routes in (*SYSTEM_SENDERS, *NEWS_SENDERS):
        got = receivers(notifiers, routes)
        out.append(f"      {label:30}  {', '.join(got) or UNSEEN}")
        if not got:
            orphans.append(f"{label}: {', '.join(routes)}")

    out.append("")
    if orphans:
        out.append(f"{len(orphans)} Meldung(en) haben keinen Empfänger:")
        out += [f"  * {entry}" for entry in orphans]
        out += [
            "",
            "Beheben: in der Watch einen Kanal anklicken, der oben aufgeführt ist —",
            "oder in config.yaml beim Notifier die fehlende Route ergänzen.",
        ]
    else:
        out.append("Alles hat einen Empfänger.")
    return "\n".join(out) + "\n"


async def report() -> str:
    from app.db import get_sessionmaker
    from app.notify.service import load_notifiers

    async with get_sessionmaker()() as session:
        notifiers = await load_notifiers(session)
        watches = list((await session.scalars(select(Watch).order_by(Watch.id))).all())
        scans = list((await session.scalars(select(ProductScan).order_by(ProductScan.id))).all())
    return render(notifiers, watches, scans)


def main() -> None:
    print(asyncio.run(report()), end="")


if __name__ == "__main__":
    main()
