# TCG Tracker

Self-hosted tracker for **Pokémon TCG** and **One Piece Card Game** collectors:

1. **Stock monitor** — polls European online shops and pings you on Discord the
   moment a watched product **first appears** (new listing / preorder) or is
   **back in stock**.
2. **News aggregator** — pulls new-set news from official sites and community
   feeds into one timeline, with `NEW_SET_ANNOUNCED`, `PREORDER_LIVE` and
   `RELEASE_SOON` alerts.
3. **Dashboard** — a dark "Holo Foil" web UI to manage watches, browse news and
   review notification history. Built for 24/7 operation on a small VPS.

Stack: Python 3.12 · FastAPI · APScheduler · SQLAlchemy (SQLite/Postgres) ·
httpx + selectolax (optional Playwright) · Jinja2 + HTMX.

---

## Quick start (Docker)

```bash
git clone <this repo> && cd tcg-tracker
cp .env.example .env              # set DASHBOARD_PASSWORD, SECRET_KEY, webhook URLs
cp config.example.yaml config.yaml # define notifiers, news sources, seed watches
docker compose up -d --build
```

Open `http://<server>:8000`, log in with `DASHBOARD_PASSWORD`, add watches.
Health check: `GET /healthz` (unauthenticated, used by Docker/uptime monitors).

SQLite data persists in `./data/`. For Postgres:

```bash
docker compose --profile postgres up -d
# .env: DATABASE_URL=postgresql+asyncpg://tcg:tcg@db:5432/tcg
```

> **Expose it safely.** Put a reverse proxy with TLS in front (Caddy/nginx) —
> the built-in auth is a single password + signed session cookie, fine behind
> HTTPS, not fine on plain HTTP over the internet.

Bare-metal alternative (systemd): see [`deploy/tcg-tracker.service`](deploy/tcg-tracker.service).

## Local development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env && cp config.example.yaml config.yaml
uvicorn app.main:app --reload
pytest                      # 67 tests, fixture-based (no network)
ruff check . && black --check .
```

---

## Configuration

Two files, clean separation:

- **`.env`** — secrets & runtime settings: database URL, dashboard password,
  Discord webhook URLs, Cardmarket API keys, proxy, intervals. See
  [`.env.example`](.env.example) for every knob.
- **`config.yaml`** — content: notifiers + routing, news sources, seed watches.
  See [`config.example.yaml`](config.example.yaml).

Watches from `config.yaml` are seeded into the DB on first start; afterwards
manage them in the dashboard (file entries are never re-imported once present).

### Notification routing

Every event carries route keys; every notifier has route patterns:

| Event | Routes |
|---|---|
| stock events | `stock:<game>`, `channel:<name>` (per-watch), `watch:<id>` |
| news events | `news:<game>`, `source:<name>` |

Patterns: `*` (everything), `stock:*` (prefix), `stock:pokemon` (exact),
`pokemon` (bare word). Example — Pokémon drops, One Piece drops and news into
three different Discord channels:

```yaml
notifiers:
  - { name: poke,  type: discord, config: { webhook_url_env: DISCORD_WEBHOOK_URL_POKEMON },  routes: ["stock:pokemon"] }
  - { name: op,    type: discord, config: { webhook_url_env: DISCORD_WEBHOOK_URL_ONEPIECE }, routes: ["stock:one_piece"] }
  - { name: news,  type: discord, config: { webhook_url_env: DISCORD_WEBHOOK_URL_NEWS },     routes: ["news:*"] }
```

Email and Telegram notifiers exist as stubs behind the same interface
(`app/notify/email.py`, `app/notify/telegram.py`) — each file documents how to
finish it.

### Events & anti-spam

- `NEW_LISTING` — fires when a watched URL that was 404/unlisted starts
  resolving to a real product (preorder pages included). The very first check
  of an already-listed product is a **silent baseline** so creating a watch
  never spams (opt out per watch with `notify_on_first_seen`).
- `BACK_IN_STOCK` — fires on `OUT_OF_STOCK/UNKNOWN → IN_STOCK`, subject to the
  per-watch **cooldown** (default 30 min) so a flapping shop can't ping you
  twenty times.
- `NEW_SET_ANNOUNCED` / `PREORDER_LIVE` — deduped per news source via stable
  GUIDs; each fires at most once per item.
- `RELEASE_SOON` — daily 09:00 (Europe/Berlin) scan for known release dates
  within `RELEASE_SOON_LEAD_DAYS` (default 7).

---

## Retailer adapters

| Adapter | Domain | Status | Notes |
|---|---|---|---|
| `amazon_de` | amazon.de | ✅ working | buybox/availability parsing, captcha detection |
| `mediamarkt_de` | mediamarkt.de | ✅ working | JSON-LD first |
| `saturn_de` | saturn.de | ✅ working | same platform as MediaMarkt |
| `cardmarket` | api.cardmarket.com | ✅ working | **official API** (OAuth 1.0a), never scrapes |
| `games_island` | games-island.eu | ✅ working | dedicated TCG shop |
| `mueller`, `smyths_de`, `gamestop_de`, `kaufland`, `gameware` | … | 🟡 stub | generic detection + per-shop TODO |
| `pokemon_center_eu`, `bandai_store` | … | 🟡 stub | need Playwright (anti-bot), TODO inside |
| `pokemon_center_queue` | pokemoncenter.com | ✅ working | **queue/waiting-room alarm** — pings when the Queue-it waiting room goes live (select via adapter dropdown; detects the queue redirect, not product stock) |
| `generic` | any other domain | ✅ fallback | JSON-LD → buy-button/sold-out text → price |

Unknown domains automatically get the **generic adapter**, so you can watch
almost any shop out of the box; dedicated adapters just make detection sharper.

### Cardmarket

Create an app at <https://api.cardmarket.com>, put the four tokens in `.env`,
then create a watch with adapter `cardmarket` and either:

- URL `https://api.cardmarket.com/ws/v2.0/output.json/products/<idProduct>`, or
- any URL + detection config `{"id_product": <idProduct>}` (dashboard: Adapter → Cardmarket).

`min_articles` in the detection config raises the "in stock" threshold.

### Adding an adapter

1. Create `app/monitor/adapters/myshop.py`:

   ```python
   from app.monitor.base import PageResult, RetailerAdapter, StockResult

   class MyShopAdapter(RetailerAdapter):
       slug = "myshop"
       name = "My Shop"
       domains = ("myshop.de", "www.myshop.de")
       # fetcher = "playwright"   # for JS-heavy sites

       def parse(self, page: PageResult) -> StockResult:
           ...  # return StockResult(status=..., price=..., title=...)
   ```

2. Register the class in `app/monitor/registry.py` (`ADAPTER_CLASSES`).
3. Add a saved-HTML fixture + parse test in `tests/` (see `test_adapters.py`).

Building blocks in `app/monitor/detection.py`: `parse_json_ld`,
`detect_stock`, `parse_german_price`, sold-out phrase lists (DE/EN).

### Politeness & robustness

Per-domain concurrency cap (default 1), randomized jitter on every request and
schedule, exponential backoff on errors/429/503, realistic User-Agent,
robots.txt honored (cached 24 h; API adapters exempt), optional proxy via
`PROXY_URL`. Every check is logged and stored in `stock_checks`. A failing
adapter records the error on the watch — the scheduler never dies.

**Play nice:** keep intervals ≥ 3–5 min, respect retailer ToS, and always
prefer an official API (like Cardmarket's) over scraping when one exists.

---

## Heartbeat, price alerts, priority pings

- **Heartbeat** — a daily "✅ Tracker läuft" message (default 09:15 local time,
  `HEARTBEAT_HOUR` / `HEARTBEAT_ENABLED`) with 24h stats and any watches or
  scanners stuck in an error state. Routes as `system:heartbeat` (matched by
  `*` or `system:*` notifier routes). Silence from the tracker no longer looks
  like "no restocks".
- **Price alerts** — set a per-watch price target ("Preisalarm ab"); a
  `PRICE_DROP` ping fires once when the product is in stock at/below the
  target and rearms when the price rises above it. Every check already records
  the price, so the 📈 button on each watch shows a price-history sparkline
  and recent checks. A restock ping at/below target arms the alert so you
  don't get two messages for one event.
- **Priority pings** — tick "Wichtig" on a watch or scanner to have its
  Discord messages mention `@everyone` (real phone push). Optional
  `QUIET_HOURS=22-7` suppresses non-priority notifications at night —
  priority events, tests and the heartbeat always go through; suppressed ones
  still show up in the history.

## Keyword scanner (new-product discovery)

The **Scanner** page watches shop *listing pages* (category, search or
new-arrivals URLs) instead of single products. Give it a URL plus keywords
(comma = AND, accent-insensitive, with optional exclude terms) and it notifies
the moment a **new** matching product shows up — e.g. the first shop listing a
freshly announced set. The first run silently baselines everything already on
the page; a sudden flood of hits (layout change) is collapsed into one summary
message. Parsing uses schema.org ItemList/Product JSON-LD when present, with a
generic product-link heuristic as fallback; a per-scanner Playwright toggle
handles JS-heavy shops.

## Shop catalog ("Auch checken" links)

`app/shops.py` ships a curated catalog of major EU Pokémon/One Piece retailers
(dedicated TCG shops with established Trustpilot profiles — Card Corner,
Games Island, Fantasywelt, Gate to the Games, CardsRfun, TCG-Trade,
Cardmarket — plus Amazon.de, MediaMarkt, Saturn, Müller, Smyths, Kaufland).

- Every **restock/new-listing alert** gets an "Auch checken" field with
  product-search links across the other catalog shops (disable with
  `SHOW_OTHER_SHOPS=false`).
- The dashboard **Shops** page lists the catalog with Trustpilot links and a
  search box that opens the product search on every shop at once.

## News sources

Config-driven in `config.yaml`; three source types:

- `rss` — any RSS/Atom feed (community news, release calendars). Items are
  filtered by set/product keywords and classified per game.
- `pokemon_official` — scrapes the pokemon.com news index (use the `/de/`
  URL for German/EU-flavored info).
- `onepiece_official` — scrapes the official One Piece Card Game information
  page (PRODUCTS entries).

Add a source type by implementing `NewsSource.fetch_items()` in
`app/news/sources/` and registering it in `app/news/service.py`
(`SOURCE_CLASSES`).

---

## Architecture

```
app/
├── main.py            FastAPI app, lifespan (migrations → seed → scheduler)
├── config.py          .env Settings + config.yaml models
├── models.py          watches, stock_checks, set_news, notifications, …
├── scheduler.py       APScheduler jobs (per watch, per news source, daily scan)
├── events.py          Event dataclass + route matching
├── monitor/
│   ├── base.py        RetailerAdapter / PageResult / StockResult
│   ├── fetchers.py    httpx + Playwright, robots.txt, backoff, throttling
│   ├── detection.py   JSON-LD / buy-button / price strategies
│   ├── registry.py    domain → adapter resolution
│   ├── service.py     check pipeline + NEW_LISTING / BACK_IN_STOCK logic
│   └── adapters/      amazon_de, mediamarkt_de, cardmarket, games_island, stubs…
├── news/              sources (rss, official sites) + ingest/dedupe/events
├── notify/            Notifier interface, Discord embeds, email/telegram stubs
└── web/               routes, Jinja2 templates, Holo Foil CSS, vendored HTMX
```

One process, three subsystems, one SQLite/Postgres DB, one notification layer.
Timestamps are stored UTC and rendered in `TIMEZONE` (default Europe/Berlin).
Schema migrations run automatically at startup (Alembic).
