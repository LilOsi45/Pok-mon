"""Look up Cardmarket product ids by name.

    docker compose exec -T app python -m app.cardmarket_find "Konoha Shido Display"

Prints candidates with their id, price guide and Cardmarket URL. Copy the id of
the *right language edition* into the watch's "Cardmarket-Produkt-ID" field —
that pins both the product and the language for the reference price.
"""

from __future__ import annotations

import asyncio
import sys

from app.cardmarket import find_products


def _money(value: float | None) -> str:
    return f"{value:.2f} €" if value is not None else "—"


async def run(search: str, id_game: int | None) -> None:
    matches = await find_products(search, id_game=id_game)
    if not matches:
        print(
            f"Keine Treffer für {search!r}.\n"
            "Mögliche Gründe: API-Zugangsdaten fehlen in der .env, der Suchbegriff\n"
            "ist zu speziell (kürzer versuchen), oder Cardmarket führt das Produkt nicht."
        )
        return
    print(f"{len(matches)} Treffer für {search!r}:\n")
    for product in matches:
        print(f"  ID {product.id_product}  —  {product.name}")
        print(f"     Trend {_money(product.trend)}   ab {_money(product.low)}")
        if product.url:
            print(f"     {product.url}")
        print()
    print('Die passende ID im Dashboard bei der Watch unter "Cardmarket-Produkt-ID" eintragen.')


def main() -> None:
    if len(sys.argv) < 2:
        print('Aufruf: python -m app.cardmarket_find "<Produktname>" [idGame]')
        raise SystemExit(2)
    search = sys.argv[1]
    id_game = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else None
    asyncio.run(run(search, id_game))


if __name__ == "__main__":
    main()
