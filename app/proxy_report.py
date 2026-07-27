"""What the proxy is actually costing today.

    docker compose exec -T app python -m app.proxy_report

Residential proxies bill per gigabyte, so the only number that matters is how
many megabytes went through them — not how many watches you have. This reads
the running app and contacts nothing.
"""

from __future__ import annotations

import asyncio

APP_URL = "http://127.0.0.1:8000/healthz/shops"


async def fetch_state() -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(APP_URL)
    resp.raise_for_status()
    return resp.json()


def render(state: dict) -> str:
    proxy = state.get("proxy") or {}
    if not proxy.get("configured"):
        return (
            "Kein Proxy eingerichtet — es läuft alles über die Server-IP und kostet nichts.\n"
            "Zum Aktivieren PROXY_URL in die .env eintragen."
        )

    today = proxy.get("today", {})
    budget = proxy.get("budget", {})
    out = [
        f"Heute über den Proxy: {today.get('requests', 0)} Abrufe, {today.get('megabytes', 0)} MB",
        f"Geschätzte Kosten heute: {today.get('estimated_cost', 0):.2f} $ "
        f"(hochgerechnet {today.get('estimated_cost', 0) * 30:.2f} $/Monat)",
        f"Rest heute: {budget.get('requests_left', 0)} Abrufe, "
        f"{budget.get('megabytes_left', 0)} MB",
        "",
    ]

    # In "always" mode nothing is ever *escalated*, so listing escalations alone
    # would report "nothing uses the proxy" while every request goes through it.
    if proxy.get("mode") == "always":
        out.append("Modus: alle Shops laufen über den Proxy.")
        if not today.get("requests"):
            out.append(
                "Noch keine Abrufe gezählt — entweder läuft der neue Build noch nicht\n"
                "oder es war noch keine Prüfung fällig."
            )
    else:
        via = proxy.get("via_proxy") or []
        if via:
            out.append("Läuft über den Proxy (alles andere direkt und kostenlos):")
            out.extend(f"  {entry}" for entry in via)
        else:
            out.append("Nichts läuft über den Proxy — kein Shop hat uns abgewiesen.")
    out.append("")

    per_shop = proxy.get("per_shop") or {}
    if per_shop:
        out.append(f"{'Shop / Art':34} {'Abrufe':>7} {'MB':>8}")
        for name, used in sorted(per_shop.items(), key=lambda kv: -kv[1].get("megabytes", 0)):
            out.append(
                f"{name[:34]:34} {used.get('requests', 0):7d} {used.get('megabytes', 0):8.2f}"
            )
    return "\n".join(out)


async def report() -> str:
    try:
        return render(await fetch_state())
    except Exception as exc:
        return f"App nicht erreichbar ({type(exc).__name__}) — läuft der Container?"


def main() -> None:
    print(asyncio.run(report()))


if __name__ == "__main__":
    main()
