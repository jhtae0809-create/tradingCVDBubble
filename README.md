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
  [Gateway setup](#set-up-ib-gateway-live-path-only) ·
  [Run it: live data](#run-it-a-live-data) ·
  [Run it: bundled dataset](#run-it-b-bundled-dataset-no-accounts-needed) ·
  [What is where](#what-is-where) ·
  [How it works](#how-it-works) · [Known issues](#known-issues--current-limitations) ·
  [When it doesn't work](#when-it-doesnt-work)

---

## Requirements

| Requirement | Notes |
|---|---|
| **Python 3.11 – 3.14** | Verified on 3.12.4. Check with `python3 -V`. |
| **MongoDB running on `localhost:27017`** | Stores every candle tier, raw tick and L2 snapshot. Nothing works without it. |
| **IBKR account with market data + IB Gateway or TWS**, logged in, API enabled | Source of tick-by-tick trades and quotes. Step-by-step: [Set up IB Gateway](#set-up-ib-gateway-live-path-only). |
| **FinViz Elite account** | Supplies the consolidated 1-minute bars that scale the tick stream — most stable and easy to backfill. |

**IB Gateway / TWS port — auto-detected.** Gateway and TWS speak the *identical*
API; only the port differs, and either can be configured to use any port. The
collector probes them in order and uses whichever answers:

| Port | Default owner |
|---|---|
| 7497 | TWS paper |
| 4002 | IB Gateway paper |
| 7496 | TWS live |
| 4001 | IB Gateway live |

To force one and skip probing:

```bash
python -m ibkr.dynamic_collector --port 4002
```

With an explicit `--port` the collector dials **only** that port and fails
loudly rather than silently falling back. If nothing answers, it prints the full
checklist (logged in? API enabled? port match? 127.0.0.1 trusted?) instead of a
bare connection error.

---

## Install

**1) MongoDB — install and start it (once).** Skip if you already have it.

| | |
|---|---|
| macOS | `brew tap mongodb/brew && brew install mongodb-community`<br>`brew services start mongodb-community` |
| Windows | Installer (recommended): <https://www.mongodb.com/try/download/community> — keep **"Install MongoDB as a Service"** ticked, which is the default, so it starts on boot.<br>Or: `winget install MongoDB.Server` |
| Ubuntu | `sudo apt install -y mongodb && sudo systemctl start mongodb` |
| Docker | `docker run -d -p 27017:27017 --name mongo mongo:7` |

Verify it is up — this must print `{ ok: 1 }`:

```bash
mongosh --quiet --eval "db.adminCommand({ping:1})"
```

> On Windows `mongosh` is a **separate download** ([MongoDB Shell]) and is not
> always on `PATH`. If the command is not found, skip it — `start_all.py` checks
> the connection itself.

[MongoDB Shell]: https://www.mongodb.com/try/download/shell

**2) The code.**

```bash
git clone https://github.com/jhtae0809-create/tradingCVDBubble
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

If `py` is not available, use `python` instead.

### Credentials

Copy `.env.example` to `.env` and put your FinViz Elite login in it:

```
FINVIZ_USERNAME = your@email.com
FINVIZ_PASSWORD = your-password
```

That is all that is needed. The FinViz auth token lives in
`finviz/api_keys.py`, which is generated — it is created automatically and
refreshed with your `.env` credentials whenever it expires, so a fresh clone
never has it and never needs it. To check the login by hand:

```bash
python -m finviz.finviz_curl          # logs in with .env, writes the token
```

Without `.env` the dashboard still runs on IBKR data alone, but shows a red
`FinViz unavailable — volume shown is UNSCALED tick volume` warning: the live
tick feed carries only ~7–14% of consolidated volume, so unscaled volume looks
normal while being several times too small.

---

## Set up IB Gateway (live path only)

Gateway and TWS speak the identical API; Gateway is just the smaller of the
two, so it is what these instructions assume.

**1) Download and install.**
<https://www.interactivebrokers.com/en/trading/ibgateway-latest.php>

**2) Log in — use a paper account.**

Pick **IB API** (not FIX) as the mode, then log in. A paper account is strongly
preferred: a live login triggers two-factor authentication every session, which
makes an unattended collector impractical. Paper still gets real-time data — it
inherits the parent live account's market-data subscriptions, so "paper" applies
to orders, not data.

**3) Enable the API.** *Configure → Settings → API → Settings*

| Setting | Set it to | Why |
|---|---|---|
| **Enable ActiveX and Socket Clients** | **ticked** | The API is off by default. Nothing connects until this is on — the one box you have to change. |
| **Read-Only API** | either way | Every collector connects with `readonly=True`, so it works ticked or unticked. Ticked is worth preferring on someone else's account: the code then provably cannot place an order. |
| **Socket port** | leave whatever is there | Auto-detected — see the port table in [Requirements](#requirements). |
| **Trusted IPs** | contains `127.0.0.1` | Usually there by default. Check it if the collector is refused at the socket rather than timing out. |

**4) Leave Gateway running, and turn off the daily auto-logoff.**

The collector needs the connection continuously: downtime is a permanent hole in
tick history that cannot be backfilled later. By default Gateway logs itself out
once every 24 hours, so an unattended collector dies overnight. Under *Configure
→ Lock and Exit*, choose **Auto restart** rather than Auto logoff — Gateway then
reconnects without asking for the password again.

### One session per market-data subscription

A market-data subscription permits **one live session at a time**, and paper
shares its parent live account's. So the same account logged in anywhere else —
another machine, a phone app, the live account while the paper one runs — takes
the feed away from this one. It is not a code failure, and it produces a
distinctive log line:

```
cdebug: QUERY | WARNING | Query error | 4;;NVDA@SMART Trades;;1;;true;;0;;I
      | Trading TWS session is connected from a different IP address
```

The API-side symptoms are **error 10189** (tick-by-tick refused) and **error
162** (historical bars refused), both worded as if the request itself were
wrong. If either appears for every ticker at once, suspect a second session
before suspecting the request.

### What the account has to be subscribed to

| Feed | Needed for | Without it |
|---|---|---|
| US real-time equities (e.g. **US Securities Snapshot and Futures Value Bundle**) | trade + quote ticks | error 10189; no CVD at all |
| **NASDAQ TotalView-OpenView** | Level-2 depth | error 309 / 10089; heatmap and S&R stay empty, everything else works |

Depth over **SMART** aggregates whichever venues the account is permitted, so a
partial subscription still draws a partial book rather than nothing. Depth from
a single exchange (`NASDAQ.NMS`) additionally needs the **TotalView-OpenView
EDS** add-on, which licenses display outside TWS itself — without it that
request is refused with `10089: requires additional subscription for API` even
when plain TotalView is subscribed.

---

## Run it (A): live data

The real system: it connects to Interactive Brokers, classifies every trade
tick-by-tick as it happens, and builds the CVD / Level-2 view from that live
feed.

```bash
python start_all.py                  # collector + dashboard
python stop_all.py                   # stop both
```

Works the same on macOS, Linux and Windows. Run it with the venv's interpreter
(`./venv_main/bin/python`, or `venv_main\Scripts\python` on Windows) or activate
the venv first — the launcher starts both processes with **whatever interpreter
runs it**. `./start_all.sh` / `./stop_all.sh` are thin wrappers around the same
Python launcher.

Before starting, the launcher checks the Python version and dependencies, checks
MongoDB is reachable, creates `finviz/api_keys.py` if missing, and refuses to
start a **second** collector — a duplicate would reuse clientId 40 and the two
IB connections would fight over it.

The whole live system is **two processes**:

1. **Collector** — `python -m ibkr.dynamic_collector`. One IB connection
   handling ticks → 1-second bars, a one-time catch-up backfill, and L2 depth.
   It is **on demand**: nothing is collected until you search a ticker in the
   app. It keeps the 5 most recently viewed tickers (3 for depth).
2. **App** — `python -m app` (<http://127.0.0.1:8050>).

Open <http://127.0.0.1:8050> and search a ticker (e.g. **NVDA**). Switch the
timeframe to **raw_tick** to confirm live ticks are arriving from Gateway.

> **A brand-new ticker starts empty.** The collector only has data from the
> moment you search it, and the 1-second backfill takes up to a minute. Outside
> market hours a freshly searched ticker may stay near-empty — expected, not a
> failure.

---

## Run it (B): bundled dataset, no accounts needed

A real slice of the project's own data is bundled in `demo_data/` (NVDA, trading
day **2026-07-22**: tick-classified 1-second bars plus the real Level-2 order
book), so the dashboard renders without a broker connection. Useful for looking
at the chart itself; it proves nothing about the live pipeline.

```bash
pip install -r requirements-demo.txt    # shorter list; optional
python -m scripts.demo_dataset load     # ~4 s, loads demo_data/
python -m app                           # starts the dashboard
```

Open **<http://127.0.0.1:8050>** and, **in this order**:

1. Type **`NVDA`** into **Search Ticker** and press Enter.
2. Leave **Base Data Source** on **`Tiered: IBKR ticks + history (default)`**.
3. Type **`2026-07-22 10:00`** into **Jump to (ET)** and press **Jump**.
4. Set **Active Timeframe** to **`1min`**.
5. Set **L2 Depth** to **`20 levels`** for the heatmap and S&R lines.

You should see candles with no background shading (real tick data), the three
CVD lines, Z-Score bubbles, and the depth heatmap behind the candles. The title
reads `NVDA — 1min (Source: ibkr_tick 84% + finviz 16%)`.

> **Step 3 is not optional.** The dataset is a fixed historical day, so the
> default live view lands on dates the bundle does not cover and the chart is
> legitimately empty until you jump.

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
   whole CVD curve. It is detected, neutralized (buy = sell, delta = 0) and
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
resolution, with the resting order book alongside it — an input to other work
rather than a strategy in itself:

- **Short-squeeze detection** — the original motivation. Forced buying by short
  sellers would appear here as aggressive buying absorbed at a wall until the
  wall breaks. **This is not implemented**: there is no short-interest, float or
  borrow-rate data anywhere in this system. What exists is the measurement layer
  such a detector needs.
- **Execution quality** — when working a large order, whether you are the
  aggressor moving the price against yourself, and where the resting liquidity
  you are about to consume actually sits.
- **Order-flow research** — the tiered store, the classified tick archive and
  the quality flags are reusable for studies unrelated to this chart.

## Known issues & current limitations

### Data completeness
1. **Level-2 depth is not the full order book.** We request 20 levels per side,
   and IBKR **SMART depth** returns up to ~20 levels within roughly ±5% of price
   — Bookmap-*style*, but not the full Bookmap book (hundreds of levels). Going
   deeper needs single-exchange TotalView deep book (trading cross-exchange
   aggregation for depth) or a dedicated market-depth vendor.
2. **Backfilled 1-second data is incomplete (~10–15% of the consolidated tape,
   IEX-biased).** Historical 1-second bars from IBKR are thin and after-hours
   backfill bars are zero-volume filler, so they differ noticeably from
   TradingView. **Live-collected** 1-second data during regular hours is
   accurate. Practical effect: price/trend over backfilled regions is usable,
   but **CVD there is a BVC estimate, not measured aggressor flow** — the chart
   shades those regions.
3. **The collector must run continuously for complete 1-second history.**
   Downtime is a **permanent gap**; full 1-second history cannot be re-backfilled
   after the fact. After-hours tick data is also thin (odd lots, lagging prices).

### Level-2 support/resistance display
4. **A side shows no S&R line when the book is genuinely one-sided.** A line is
   drawn only for a wall passing the persistence + size threshold, so no
   meaningful wall means no line — correct, not a bug. S&R is computed on the
   **full book** and both line prices are included in the price-axis fit, so the
   10/20-level selector never hides a real wall.
5. **S&R side classification depends on bid/ask dominance in the captured
   window.** If the last close drifts away from the resting book, the
   classification can skew to one side.

### Closing-auction detection
6. **The auction condition code is confirmed for NVDA only.** The closing-cross
   token `'6'` was verified against the real NVDA 16:00 print; other tickers or
   venues may stamp `'M'` / `'X'`. The volume heuristic still catches those. Run
   `python -m scripts.inspect_auction_conditions --ticker <SYM>` after a close to
   confirm a new ticker's code.

### App / operational
7. **A brand-new 1-second ticker shows "Fetching…" for up to ~1 minute** while
   its backfill runs. An unrecognized symbol shows "Unknown ticker" via a FinViz
   probe, so a symbol that exists on IBKR but is not listed by FinViz could be
   misflagged.
8. **After restarting the app, hard-refresh the browser** — a stale Dash
   callback spec otherwise leaves requests stalled.
9. **The bundled dataset covers one ticker and one day** (NVDA, 2026-07-22);
   other tickers or dates render empty until live collection fills them in.
10. **All state lives in MongoDB, outside the repo.** Copying the project
    directory carries no data with it, and two checkouts on one machine share a
    database — so a copy can look like it works purely because the original's
    data is still in Mongo. On a new machine the chart is blank until you load
    the bundled dataset or run the collector.

---

## When it doesn't work

### First: the 30-second sanity check

Run these with the venv's interpreter (`./venv_main/bin/python`, or
`venv_main\Scripts\python` on Windows). They isolate almost every setup failure:

```bash
python -V                                             # must be 3.11 - 3.14
python -c "import dash, pandas, numpy, pymongo; print('deps OK')"
python -c "from pymongo import MongoClient; print('mongo:', MongoClient('mongodb://localhost:27017/', serverSelectionTimeoutMS=3000).admin.command('ping'))"
python -c "from pymongo import MongoClient; print('bars in DB:', MongoClient('mongodb://localhost:27017/')['finviz_db']['candles'].count_documents({}))"
```

(These use `pymongo` rather than `mongosh` so they work where the shell is not
installed — notably on Windows.)

If the last one prints `0`, the database is empty and the app has nothing to
draw. That is not a crash; run the collector or load the bundled dataset.

### Install failures

| Symptom | Cause / fix |
|---|---|
| `No matching distribution found for pandas==...` | **Python too old** (e.g. macOS's built-in `/usr/bin/python3`, which is 3.9). Use 3.11–3.14: `brew install python@3.12`, or pyenv. |
| `ResolutionImpossible`, or compiler errors building numpy/pandas from source | pip fell back to a source build because no wheel matched your Python. Fix the Python version rather than installing a compiler. |
| `ModuleNotFoundError: No module named 'dash'` | Dependencies installed into a *different* interpreter than the one running the app. Check `which python3` matches the venv you installed into. |

### Startup / runtime failures

| Symptom | Cause / fix |
|---|---|
| `ServerSelectionTimeoutError`, or every chart request stalls ~30 s then errors | **MongoDB is not running.** `brew services start mongodb-community` (macOS) or `docker start mongo`. |
| Chart is empty, no error | Almost always an empty database, or a time range with no data. In demo mode you **must** jump to `2026-07-22`. |
| `Address already in use` on port 8050 | Another instance is running. `python stop_all.py`, or `PORT=8051 python -m app`. |
| Chart stalls after restarting the app | Hard-refresh the browser — a stale Dash callback spec leaves requests pending. |
| No L2 heatmap | Set **L2 Depth** to 10 / 20 / Full, and check you are inside a time range that has depth snapshots. |
| `./start_all.sh: Permission denied` | `chmod +x start_all.sh stop_all.sh`. Or skip the wrapper: `python start_all.py`. |
| Nothing works on Windows | Use `python start_all.py` / `python stop_all.py`; the `.sh` wrappers are macOS/Linux only, and the venv is `venv_main\Scripts\`. |

### Live-mode (IBKR) failures

| Symptom | Cause / fix |
|---|---|
| Nothing is collected, no connection in the log | Check `logs_collector.log` — on failure it lists every port it tried and why. Usual causes: Gateway not **logged in**, "Enable ActiveX and Socket Clients" unticked, or 127.0.0.1 missing from Trusted IPs. |
| Connected to the wrong instance (e.g. paper when you wanted live) | Both were listening and the probe took the first. Pin it: `python -m ibkr.dynamic_collector --port 7496`. |
| `Trading TWS session is connected from a different IP address`, and 10189 / 162 for **every** ticker at once | The same account is logged in somewhere else — see [One session per market-data subscription](#one-session-per-market-data-subscription). |
| `error 10189 — requested market data is not subscribed` | Real-time market-data permission is missing or was lost (it can drop when the account session changes). Re-check in Account Management. |
| `error 309 / not subscribed` on depth | No market-depth subscription (e.g. Nasdaq TotalView). Depth is unavailable; the rest still works. |
| `10089: requires additional subscription for API` on depth only | Single-exchange depth (`NASDAQ.NMS`) needs the **TotalView-OpenView EDS** add-on on top of TotalView. SMART depth still works. |
| `clientId already in use` / collector keeps disconnecting | Two collectors are running. `python stop_all.py`, then `python start_all.py`. |
| Data only appears for the ticker you searched | By design — collection is on demand, most-recent 5 tickers (3 for depth). |
| A ticker searched outside market hours stays empty | Expected: there are no live ticks, and the 1-second backfill is thin outside regular hours. |

---

## Credit

Based on initial code by Aisiri Cherrimane Narendra —
[github.com/aisiricherrimane](https://github.com/aisiricherrimane)
