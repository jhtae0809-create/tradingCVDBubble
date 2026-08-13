# Trading CVD Bubble

A **Cumulative Volume Delta (CVD)** dashboard for reading order-flow pressure
behind a stock's price.

A candlestick chart tells you *where* price went. This tool tells you *how hard
it was pushed and by whom*: every individual trade is classified as
buyer-initiated or seller-initiated from the live bid/ask quote, accumulated
into a CVD curve, and drawn together with the resting order book (Level-2
depth) as a Bookmap-style heatmap. Aggressive flow meeting a passive wall is
where price stalls, reverses, or breaks out — the chart is designed to make
that moment visible.

Underneath it is supply and demand: a price chart shows only the *result* of the
imbalance, after the fact. This measures the imbalance itself, as it
accumulates. What it produces is a **measurement layer**, not a trading
strategy — see [Where this could be applied](#where-this-could-be-applied).

- **[USAGE.md](USAGE.md) — how to read the chart** (every color, line and control).
- Jump to: [Requirements](#requirements) · [Install](#install) ·
  [Run it: live data](#run-it-a-live-data) ·
  [Run it: bundled dataset](#run-it-b-bundled-dataset-no-accounts-needed) ·
  [What is where](#what-is-where) ·
  [How it works](#how-it-works) · [Known issues](#known-issues--current-limitations) ·
  [When it doesn't work](#when-it-doesnt-work)

---

## Requirements

**Everything in this table marked _required_ is needed for both run paths
below.** The dashboard never reads data files directly — it reads a local
**MongoDB**, which is what the collector (or the bundled dataset loader) fills.
That is the single most common reason a fresh clone shows an empty chart.

| | Requirement | Notes |
|---|---|---|
| **required** | **Python 3.11 – 3.14** | Verified on 3.12.4. Check with `python3 -V`. On 3.10 or older the install fails — see [When it doesn't work](#when-it-doesnt-work). |
| **required** | **MongoDB running on `localhost:27017`** | Stores every candle tier, raw tick and L2 snapshot. Nothing works without it. |
| live only | **IBKR account + IB Gateway or TWS**, logged in, API enabled | Source of tick-by-tick trades and quotes. |
| live only | **Market-data subscription** (e.g. Nasdaq TotalView) | Without it, depth requests fail (`error 309`) and quotes are delayed. |
| **required for correct volume** | **FinViz Elite account** | Supplies the consolidated 1-minute bars that scale the tick stream — roughly a tenth of the tape — up to real traded volume. The app still *draws* without it, but every volume and CVD figure is then several times too small, so it is not an optional extra. Missing or broken, it is reported in red on the dashboard. |

**IB Gateway / TWS port — auto-detected.** Gateway and TWS speak the *identical*
API; only the port differs, and either application can be configured to use any
port. So the collector probes them in order and uses whichever answers:

| Port | Default owner |
|---|---|
| 7497 | TWS paper |
| 4002 | IB Gateway paper |
| 7496 | TWS live |
| 4001 | IB Gateway live |

You do not normally need to configure anything. To force one (and skip probing):

```bash
python -m ibkr.dynamic_collector --port 4002
```

With an explicit `--port` the collector dials **only** that port and fails
loudly rather than silently falling back, so a deliberate choice is never
overridden. If nothing answers, it prints the full checklist (Gateway logged in?
API enabled? port match? 127.0.0.1 trusted?) instead of a bare connection error.

---

## Install

Common to both run paths.

**1) MongoDB — install and start it (once).**

| | |
|---|---|
| macOS | `brew tap mongodb/brew && brew install mongodb-community`<br>`brew services start mongodb-community` |
| Windows | Installer (recommended): <https://www.mongodb.com/try/download/community> — keep **"Install MongoDB as a Service"** ticked, which is the default, so it starts on boot.<br>Or: `winget install MongoDB.Server` |
| Ubuntu | `sudo apt install -y mongodb && sudo systemctl start mongodb` |
| Docker | `docker run -d -p 27017:27017 --name mongo mongo:7` |

Verify it is actually up — this must print `{ ok: 1 }`:

```bash
mongosh --quiet --eval "db.adminCommand({ping:1})"
```

> On Windows `mongosh` is a **separate download** ([MongoDB Shell]) and is not
> always on `PATH`. If the command is not found, skip it — `start_all.py` checks
> the connection itself and tells you if it cannot reach the database.

[MongoDB Shell]: https://www.mongodb.com/try/download/shell

**2) The code.**

```bash
git clone <this-repo-url>
cd tradingCVDBubble
```

**macOS / Linux:**

```bash
python3 -m venv venv_main
./venv_main/bin/pip install -r requirements.txt
```

**Windows** (PowerShell or `cmd`) — the venv layout differs, `Scripts\` instead
of `bin/`:

```
py -3.12 -m venv venv_main
venv_main\Scripts\pip install -r requirements.txt
```

If `py` is not available, use `python` instead. Confirm the version first with
`python -V` — it must be 3.11–3.14.

### Credentials (`.env` and `finviz/api_keys.py`)

**Do not skip this if you have a FinViz Elite account** — without a working
token the volume on every chart is wrong (see
[Requirements](#requirements)), and the dashboard will say so in red.

There are two files, and they are *not* interchangeable:

| File | Holds | Created by |
|---|---|---|
| `.env` | `FINVIZ_USERNAME`, `FINVIZ_PASSWORD` | you: `cp .env.example .env`, then edit |
| `finviz/api_keys.py` | `FINVIZ_AUTH_TOKEN` | **auto-created** on first run |

`finviz/api_keys.py` is **gitignored and therefore absent from every clone** —
it is generated state, not source, because `finviz/finviz_curl.py` rewrites it
in place each time the token is renewed. It is now created automatically (empty)
by `start_all.py` and on first import of `finviz.new_finviz`, so you never have
to make it by hand. To fill in the token, either paste it into that file or let
the login flow fetch one for you:

```bash
python -m finviz.finviz_curl          # logs in with .env, writes the token
```

**With `.env` filled in you do not have to touch the token at all.** The file
`start_all.py` creates holds an empty one, but the first FinViz fetch sees that,
logs in with your `.env` credentials, writes the token and retries — the same
path that already handled an expired token. Running `finviz_curl` by hand is
only for checking that the login itself works.

What is *not* automatic is `.env`: without `FINVIZ_USERNAME` and
`FINVIZ_PASSWORD` there is nothing to log in with. In that case the dashboard
runs on IBKR data alone and shows a red
`FinViz unavailable — volume shown is UNSCALED tick volume` warning, because
tick volume that has not been scaled to the consolidated tape is far too low
and would otherwise look perfectly normal.

---

## Run it (A): live data

The real system: it connects to Interactive Brokers, classifies every trade
tick-by-tick as it happens, and builds the CVD / Level-2 view from that live
feed. Needs the IBKR rows in [Requirements](#requirements).

**Works the same on macOS, Linux and Windows** — the launcher is plain Python:

```bash
python start_all.py                  # collector + dashboard
python stop_all.py                   # stop both
```

Use the venv's interpreter (`./venv_main/bin/python` on macOS/Linux,
`venv_main\Scripts\python` on Windows) or activate the venv first — the launcher
starts both processes with **whatever interpreter runs it**, so that is the one
that needs the dependencies. It tells you if any are missing.

On macOS/Linux `./start_all.sh` and `./stop_all.sh` still work; they are thin
wrappers that locate an interpreter (`$PYTHON` → active venv → `./venv_main` →
`python3`) and hand off to the Python launcher, so there is one implementation.

Before starting anything, the launcher:

- checks the Python version and that the dependencies are importable;
- checks **MongoDB** is reachable, and prints platform-specific install
  instructions if not, instead of letting every chart request stall ~30 s;
- creates `finviz/api_keys.py` if missing;
- refuses to start a **second** collector — a duplicate would reuse clientId 40
  and the two IB connections would fight over it. It detects both processes it
  started itself (via `.run_pids.json`) and, on macOS/Linux, any left running by
  hand.

The whole live system is **two processes**:

1. **Collector** — `python -m ibkr.dynamic_collector`. One IB connection
   handling ticks → 1-second bars, a one-time catch-up backfill, and L2 depth.
   It is **on demand**: nothing is collected until you search a ticker in the
   app. It keeps the 5 most recently viewed tickers (3 for depth) and evicts the
   oldest.
2. **App** — `python -m app` (<http://127.0.0.1:8050>).

Open <http://127.0.0.1:8050>, search a ticker (e.g. **NVDA**), and it starts
collecting and charting from the live feed.

> **A brand-new ticker starts empty.** The collector only has data from the
> moment you search it, and the 1-second backfill takes up to a minute. Outside
> market hours a freshly searched ticker may stay near-empty — that is expected,
> not a failure.

---

## Run it (B): bundled dataset, no accounts needed

**Use this path if you don't have IBKR / FinViz accounts** — it is the only way
to see the chart populated without a broker connection, and it needs no
credentials at all.

A real slice of the project's own data is bundled in `demo_data/` (NVDA, trading
day **2026-07-22**: tick-classified 1-second bars plus the real Level-2 order
book), so the same dashboard renders from it.

```bash
pip install -r requirements-demo.txt    # shorter list; optional
python -m scripts.demo_dataset load     # ~4 s, loads demo_data/
python -m app                           # starts the dashboard
```

> Run these with the venv's interpreter: prefix `./venv_main/bin/` on
> macOS/Linux or `venv_main\Scripts\` on Windows, or activate the venv first
> (`source venv_main/bin/activate` / `venv_main\Scripts\activate`).

Open **<http://127.0.0.1:8050>** and, **in this order**:

1. Type **`NVDA`** into **Search Ticker** and press Enter.
2. Leave **Base Data Source** on **`Tiered: IBKR ticks + history (default)`**.
3. Type **`2026-07-22 10:00`** into **Jump to (ET)** and press **Jump**.
4. Set **Active Timeframe** to **`1min`**.
5. Set **L2 Depth** to **`20 levels`** for the order-book heatmap and the
   support/resistance lines.

You should see candles with no background shading (real tick data), the three
CVD lines, Z-Score bubbles, and the depth heatmap behind the candles. The chart
title reads `NVDA — 1min (Source: ibkr_tick 84% + finviz 16%)`.

> **Step 3 is not optional.** The dataset is a fixed historical day, so the
> default "live" view lands on a date range the bundle does not cover and the
> chart is legitimately empty until you jump. This is the single most common
> reason demo mode looks broken.

> `requirements-demo.txt` is the short list needed only to *view* data (no
> broker library, no scraping stack). The full `requirements.txt` from
> [Install](#install) already covers it.

---

## What is where

```
tradingCVDBubble/
├── app.py                  THE DASHBOARD. Dash app + all UI callbacks (port 8050)
├── start_all.py            starts collector + dashboard;  stop_all.py stops them
│                        (cross-platform; the .sh wrappers just call these)
│
├── ibkr/                   DATA COLLECTION from Interactive Brokers
│   ├── dynamic_collector.py  the one collector that is actually run: ticks,
│   │                         1-sec bars, catch-up backfill and L2 depth over a
│   │                         single IB connection, on demand per searched ticker
│   ├── tick_collector.py     tick subscription → 1-second bar aggregation
│   ├── backfill.py           historical 1-second bars (reqHistoricalData)
│   ├── level2_collector.py   standalone depth collector (superseded, kept for reference)
│   ├── reclassify.py         re-classifies stored ticks with quote-lag correction
│   └── ibkr_main.py          minimal connection proof-of-concept
│
├── cvd/                    THE ANALYTICS
│   ├── aggressor.py          trade classification (Lee-Ready): quote rule →
│   │                         tick rule → zero-tick inheritance. The core of "real" CVD
│   ├── calculator.py         source-aware pipeline + closing-auction (MOC) neutralization
│   ├── visualizer.py         the whole Plotly figure: candles, CVD lines, bubbles,
│   │                         volume pies, L2 heatmap, data-quality shading
│   ├── ofi.py                Order-Flow Imbalance (Cont, Kukanov & Stoikov 2014)
│   └── dom_analyzer.py       legacy DOM/spoofing analysis (used by main.py only)
│
├── history/                STORAGE & AGGREGATION (how 1-second data stays fast)
│   ├── schema.py             tier definitions (1sec→1min→30min→1day) + quality ranks
│   ├── rollup.py             incremental, watermark-based tier rollups
│   ├── store.py              quality-guarded upserts (an estimate may never
│   │                         overwrite real tick data)
│   ├── serve.py              tier-aware serving + incremental cache for the chart
│   ├── bvc.py                Bulk Volume Classification (Easley et al. 2012)
│   └── session_grid.py       renders missing intraday bars as empty slots
│
├── level2_webapp/
│   └── data_provider.py      L2 snapshots → heatmap grid + support/resistance detection
│
├── finviz/                 CONSOLIDATED BAR FEED (FinViz Elite)
│   ├── new_finviz.py         fetch + MongoDB upsert; symbol validity probe
│   └── finviz_curl.py        automatic auth-token renewal
│
├── scripts/                VALIDATION & RESEARCH (each writes a markdown report)
│   ├── demo_dataset.py       export/load the bundled demo dataset
│   ├── backtest_correlation*.py   CVD ↔ price correlation across tickers
│   ├── validate_moc.py, inspect_auction_conditions.py   closing-auction checks
│   ├── coverage_and_latency.py    tick coverage vs. consolidated tape, feed latency
│   ├── verify_full_tape.py, ab_classify_midpoint.py   classification A/B tests
│   └── session_tf_grid.py         daily session × timeframe correlation grid
│
├── tests/mock_level2_stream.py    synthetic L2 stream (develop the heatmap offline)
├── demo_data/              the bundled NVDA sample (gzipped Extended JSON)
├── main.py                 legacy pre-dashboard FinViz-only loop
└── TradingView/            abandoned pre-IBKR scraping path (kept for history)
```

**Databases** (local MongoDB): `finviz_db.candles` holds every bar tier plus
`raw_ticks` / `raw_quotes`; `trading_cvd.level2_snapshots` holds the order-book
snapshots.

---

## How it works

```
IBKR Gateway ──┬─ trade ticks ─┐
               └─ quote ticks ─┴→ aggressor classification → 1-second bars ─┐
               └─ market depth ──→ level2_snapshots ──┐                     │
                                                      │                     ▼
FinViz Elite ────→ consolidated 1-minute bars ────────┼──────────────→ MongoDB
                                                      │                     │
                                                      │        rollup: 1sec→1min→30min→1day
                                                      ▼                     ▼
                                              heatmap + S&R  ────────→  Dash app → Plotly chart
```

Four ideas carry the project:

1. **Real aggressor classification.** Each trade is compared against the
   prevailing quote: at-or-above the ask is buyer-initiated, at-or-below the bid
   is seller-initiated, in-between falls back to the tick rule. This replaces
   the candle-shape guesswork most retail CVD tools use.
2. **Honest estimation where ticks do not exist.** Older history has bars only,
   so buy/sell is estimated with **BVC** (`buy = V · Φ(ΔP/σ)`). Every bar carries
   a `quality` flag, the chart **shades estimated regions**, and a
   quality-guarded upsert makes sure an estimate can never overwrite real tick
   data.
3. **Closing-auction neutralization.** The Market-On-Close cross is a single
   massive print at one price with no direction; left alone it dominates the
   whole CVD curve. It is detected and neutralized (buy = sell, delta = 0) and
   drawn as a gray bar.
4. **Tiered storage.** One second of data per ticker per day is ~57,600 bars, so
   bars are rolled up incrementally (1sec → 1min → 30min → 1day) with watermarks,
   and every chart timeframe is served from the nearest materialized tier
   instead of resampling from raw ticks.

Read **[USAGE.md](USAGE.md)** for what each visual element means.

---

## Validation

The methodology was checked rather than assumed; `scripts/` regenerates each
report:

```bash
python -m scripts.coverage_and_latency        # feed coverage & latency
python -m scripts.validate_moc                # closing-auction detection
python -m scripts.backtest_correlation        # CVD ↔ price correlation
python -m scripts.inspect_auction_conditions --ticker NVDA
```

Selected findings: measured feed latency is ~0.4 s (not the 15-minute delay a
delayed feed would show); the live tick stream captures ~7–14% of consolidated
volume, which is why FinViz bars are used to rescale volume; and the
closing-cross condition code `'6'` was confirmed against a real NVDA close.

---

## Where this could be applied

What the system produces is a measured supply/demand imbalance per bar, at any
resolution, with the resting order book alongside it. That is an input to other
work rather than a strategy in itself:

- **Short-squeeze detection** — the original motivation for the project. Forced
  buying by short sellers would appear here as aggressive buying absorbed at a
  wall until the wall breaks. **This is not implemented**: there is no
  short-interest, float or borrow-rate data anywhere in this system, and no
  squeeze detector. What exists is the measurement layer such a detector needs.
- **Execution quality** — when working a large order, whether you are the
  aggressor moving the price against yourself, and where the resting liquidity
  you are about to consume actually sits.
- **Order-flow research** — the tiered store, the classified tick archive and
  the quality flags are reusable for studies unrelated to this chart. The
  scripts in `scripts/` are examples.

## Known issues & current limitations

An honest running list of what is incomplete or behaves imperfectly, so it is
clear what still needs attention.

### Data completeness
1. **Level-2 depth is not the full order book.** We request 20 levels per side
   from IBKR, and IBKR **SMART depth** returns up to ~20 levels within roughly
   ±5% of price. This is much deeper than before and reads Bookmap-style, but it
   is **not** the full Bookmap book (hundreds of levels). Going deeper would
   require single-exchange TotalView deep book (which trades cross-exchange
   aggregation for depth) or a dedicated market-depth vendor.
2. **Backfilled 1-second data is incomplete (~10–15% of the consolidated tape,
   IEX-biased).** Historical 1-second bars pulled from IBKR are thin, and
   after-hours backfill bars are flat / zero-volume filler, so backfilled
   1-second candles differ noticeably from TradingView. **Live-collected**
   1-second data during regular hours is accurate; only the historical backfill
   is limited. Practical effect: price/trend over backfilled regions is usable,
   but **CVD there is a BVC/wick estimate, not measured aggressor flow** (the
   chart shades these regions — see USAGE.md).
3. **The collector must run continuously for complete 1-second history.** Any
   downtime is a **permanent gap** in tick-level data (full 1-second history
   cannot be re-backfilled after the fact). After-hours tick data is also thin
   (odd lots; prices can lag the consolidated tape).

### Level-2 support/resistance display
4. **A side may still show no S&R line when the book is genuinely one-sided.** A
   side only draws a line if it has a wall that passes the persistence + size
   threshold, so if there is no meaningful resting wall on that side at the
   moment, no line is drawn (this is correct, not a bug). *Fixed:* S&R is now
   computed on the **full book** and both line prices are included in the price-
   axis fit, so the depth selector (10 / 20 levels) no longer hides a real wall
   that sits outside the visible heatmap — both lines show whenever both walls
   exist, at any depth.
5. **S&R side classification depends on the bid/ask dominance in the captured
   window.** If the last close drifts away from the resting book, the
   classification can skew everything to one side.

### Closing-auction detection
6. **The auction condition code is confirmed for NVDA only.** The closing-cross
   token `'6'` was verified against the real NVDA 16:00 print; other tickers or
   venues may stamp a different token (`'M'` / `'X'`). The volume heuristic still
   catches those, but code-based precision is NVDA-verified only. Run
   `python -m scripts.inspect_auction_conditions --ticker <SYM>` after a close to
   confirm a new ticker's code.

### App / operational
7. **A brand-new 1-second ticker shows "Fetching…" for up to ~1 minute** while
   its backfill runs asynchronously. An unrecognized symbol now shows
   "Unknown ticker" (via a FinViz probe), but a symbol that exists on IBKR yet is
   not listed by FinViz could be misflagged.
8. **After restarting the app/server, hard-refresh the browser** — a stale Dash
   callback spec otherwise leaves requests stalled.
9. **The bundled dataset covers one ticker and one day.** `demo_data/` holds
   NVDA around 2026-07-22 only; other tickers or dates render empty until live
   collection fills them in.
10. **All state lives in MongoDB, outside the repo.** Copying or re-cloning the
    project directory carries no data with it, and two checkouts on the same
    machine share one database — so a copy can look like it "works" purely
    because the original's collected data is still in Mongo. On a new machine
    the database starts empty and the chart is blank until you load the bundled
    dataset or run the collector.
11. **The FinViz token is generated state.** `finviz/api_keys.py` is rewritten
    by `finviz_curl.py` and gitignored, so it never travels with a clone. It is
    auto-created empty now, but a clone can never inherit a working token.

---

## When it doesn't work

### First: the 30-second sanity check

Run these four. They isolate almost every setup failure:

Run them with the venv's interpreter (`./venv_main/bin/python`, or
`venv_main\Scripts\python` on Windows) — shown here as `python`:

```bash
python -V                                             # must be 3.11 - 3.14
python -c "import dash, pandas, numpy, pymongo; print('deps OK')"
python -c "from pymongo import MongoClient; print('mongo:', MongoClient('mongodb://localhost:27017/', serverSelectionTimeoutMS=3000).admin.command('ping'))"
python -c "from pymongo import MongoClient; print('bars in DB:', MongoClient('mongodb://localhost:27017/')['finviz_db']['candles'].count_documents({}))"
```

(The MongoDB checks use `pymongo` rather than `mongosh` so they work even where
the shell is not installed — notably on Windows.)

If the last one prints `0`, the database is empty — the app has nothing to draw.
That is not a crash; load the bundled dataset
([Run it B](#run-it-b-bundled-dataset-no-accounts-needed)) or run the collector.

### Install-time failures

| Symptom | Cause / fix |
|---|---|
| `ERROR: ResolutionImpossible` or `Cannot install -r requirements.txt` | **Python too new.** Older revisions of this repo pinned `numpy==1.26.4`, which has no wheels past Python 3.12, so 3.13+ could not resolve. Fixed — dependencies are now version floors. If you hit it on an old copy, use Python 3.12 or take the current `requirements.txt`. |
| `No matching distribution found for pandas==3.0.2` | **Python too old** (e.g. macOS's built-in `/usr/bin/python3`, which is 3.9). Use 3.11–3.14: `brew install python@3.12`, or pyenv. |
| `ModuleNotFoundError: No module named 'dash'` | Dependencies not installed, or installed into a *different* interpreter than the one running the app. Check `which python3` matches the venv you installed into. Older revisions of this repo also omitted `dash` from `requirements.txt` entirely — take the current file. |
| Compiler errors building numpy/pandas from source | Same root cause as row 1: pip fell back to a source build because no wheel matched your Python. Fix the Python version rather than installing a compiler. |

### Startup / runtime failures

| Symptom | Cause / fix |
|---|---|
| `ServerSelectionTimeoutError`, or every chart request stalls ~30 s then errors | **MongoDB is not running.** `brew services start mongodb-community` (macOS) or `docker start mongo`. `start_all.py` checks this up front and refuses to start. |
| `start_all.sh` prints `No such file or directory` and nothing starts | An **old copy** with a hardcoded interpreter path (`PY=/Users/<someone>/.pyenv/...`). Current `start_all.sh` resolves the interpreter from `$PYTHON` → active venv → `./venv_main` → `python3`. |
| Red banner: `⚠ Error fetching NVDA: cannot import name 'get_candle_data' from 'finviz.new_finviz'` | `finviz/api_keys.py` was missing. It is gitignored, so no clone has it. Now auto-created — see [Credentials](#credentials-env-and-finvizapi_keyspy). On an old copy, create the file with `FINVIZ_AUTH_TOKEN = ""`. |
| Chart is empty, no error | Almost always: empty database, or a time range with no data. In demo mode you **must** jump to `2026-07-22` (step 3). |
| `Address already in use` on port 8050 | Another instance is running. `python stop_all.py`, or `PORT=8051 python -m app`. |
| Chart stalls after restarting the app | Hard-refresh the browser — a stale Dash callback spec leaves requests pending. |
| No L2 heatmap | Set **L2 Depth** to 10 / 20 / Full, and check you are inside a time range that has depth snapshots. |
| `./start_all.sh: command not found` / `Permission denied` | `chmod +x start_all.sh stop_all.sh`, and run it as `./start_all.sh`. Or skip the wrapper entirely: `python start_all.py`. |
| Nothing works on Windows | Use `python start_all.py` / `python stop_all.py`; the `.sh` wrappers are macOS/Linux only. Remember the venv is `venv_main\Scripts\`, not `venv_main/bin/`. |

### Live-mode (IBKR) failures

| Symptom | Cause / fix |
|---|---|
| Nothing is collected, collector log shows no connection | The collector now probes 7497 / 4002 / 7496 / 4001 automatically, so this is no longer a port default problem. Check `logs_collector.log` — on total failure it lists every port it tried and why. Usual causes: Gateway/TWS not **logged in**, "Enable ActiveX and Socket Clients" unticked, or 127.0.0.1 missing from Trusted IPs. |
| Collector connects but to the wrong instance (e.g. paper when you wanted live) | Both were listening and the probe took the first. Pin it: `python -m ibkr.dynamic_collector --port 7496`. |
| `error 309 / not subscribed` | The IBKR account lacks a market-depth subscription (e.g. Nasdaq TotalView). Depth is unavailable; the rest still works. |
| `error 10189 — requested market data is not subscribed` | Real-time market-data permission is missing or was lost (it can drop when the account session changes). Re-check the subscription in Account Management. |
| `clientId already in use` / collector keeps disconnecting | Two collectors are running. `python stop_all.py`, then `python start_all.py` (which now refuses to start a duplicate). |
| Data only appears for the ticker you searched | By design — collection is on demand, most-recent 5 tickers (3 for depth). |
| A ticker searched outside market hours stays empty | Expected: there are no live ticks, and the 1-second backfill is thin outside regular hours. |

---

## Credit

Based on initial code by Aisiri Cherrimane Narendra —
[github.com/aisiricherrimane](https://github.com/aisiricherrimane)
