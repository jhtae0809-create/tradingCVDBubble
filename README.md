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
- Jump to: [Quick start](#quick-start-demo-mode-no-broker-account-needed) ·
  [What is where](#what-is-where) · [Live mode](#live-mode-real-time-collection) ·
  [How it works](#how-it-works) · [Known issues](#known-issues--current-limitations) ·
  [Troubleshooting](#troubleshooting)

---

## Quick start (demo mode, no broker account needed)

The dashboard reads from a local MongoDB that is normally filled by the live
IBKR collector. So that anyone can run this repository without a brokerage
account or a paid market-data subscription, a **real slice of the project's own
data is bundled in `demo_data/`** (NVDA, trading day 2026-07-22: tick-classified
1-second bars plus the real Level-2 order book).

**Prerequisites:** Python 3.12+ and a local MongoDB.

```bash
# 0) MongoDB — install and start it (once)
#    macOS:  brew tap mongodb/brew && brew install mongodb-community
#            brew services start mongodb-community
#    Ubuntu: sudo apt install mongodb && sudo systemctl start mongodb
#    Docker: docker run -d -p 27017:27017 --name mongo mongo:7

git clone <this-repo-url>
cd tradingCVDBubble

python3 -m venv venv_main
./venv_main/bin/pip install -r requirements-demo.txt

./venv_main/bin/python -m scripts.demo_dataset load   # ~4 s, loads demo_data/
./venv_main/bin/python -m app                         # starts the dashboard
```

Open **<http://127.0.0.1:8050>** and:

1. Type **`NVDA`** in the ticker box.
2. Set **Data source → Tick (IBKR)** and **Active Timeframe → 1min**.
3. Type **`2026-07-22 10:00`** into **Jump to (ET)** and press **Jump**.
4. Turn **L2 Depth** to **20 levels** to bring up the order-book heatmap and the
   support/resistance lines.

You should see candles with no background shading (real tick data), the three
CVD lines, Z-Score bubbles, and the depth heatmap behind the candles.

> `requirements-demo.txt` is the short list needed to *view* data. Use the full
> `requirements.txt` if you also want to collect new data or run the research
> scripts.

---

## What is where

```
tradingCVDBubble/
├── app.py                  THE DASHBOARD. Dash app + all UI callbacks (port 8050)
├── start_all.sh            starts collector + dashboard;  stop_all.sh stops them
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

## Live mode (real-time collection)

Only needed to collect *new* market data. Requires accounts:

| Requirement | Why |
|---|---|
| **IBKR account + IB Gateway / TWS** running and logged in, API enabled on port 7497 | tick-by-tick trades and quotes |
| **Market-data subscription** (e.g. Nasdaq TotalView for depth) | without it, depth requests fail and quotes are delayed |
| **FinViz Elite account** | consolidated 1-minute bars, used to scale the thin tick-stream volume to real traded volume |

```bash
cp .env.example .env                 # then fill in FINVIZ_USERNAME / FINVIZ_PASSWORD
./venv_main/bin/pip install -r requirements.txt
./start_all.sh                       # collector + dashboard
./stop_all.sh                        # stop both
```

`start_all.sh` picks its interpreter from `$PYTHON`, then an active virtualenv,
then `./venv_main`, then `python3` on PATH — override with
`PYTHON=/path/to/python ./start_all.sh`.

The whole live system is **two processes**:

1. **Collector** — `python -m ibkr.dynamic_collector`. One IB connection
   handling ticks → 1-second bars, a one-time catch-up backfill, and L2 depth.
   It is **on demand**: nothing is collected until you search a ticker in the
   app. It keeps the 5 most recently viewed tickers (3 for depth) and evicts the
   oldest.
2. **App** — `python -m app` (<http://127.0.0.1:8050>).

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
9. **Demo mode covers one ticker and one day.** `demo_data/` holds NVDA around
   2026-07-22 only; other tickers or dates render empty until live collection
   fills them in.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError: dash` | dependencies not installed — `pip install -r requirements-demo.txt` |
| `ServerSelectionTimeoutError` on start | MongoDB is not running — start it (`brew services start mongodb-community`) |
| Chart is empty in demo mode | the demo data was not loaded — run `python -m scripts.demo_dataset load`, and make sure the ticker is **NVDA** and you jumped to **2026-07-22** |
| No L2 heatmap | set **L2 Depth** to 10/20/Full, and check you are inside the demo day's time range |
| Chart stalls after restarting the app | hard-refresh the browser (stale Dash callback spec) |
| Live mode: `error 309 / not subscribed` | the IBKR account lacks a market-depth subscription |
| Live mode: nothing is collected | IB Gateway must be logged in with the API enabled on port 7497 |

---

## Credit

Based on initial code by Aisiri Cherrimane Narendra —
[github.com/aisiricherrimane](https://github.com/aisiricherrimane)
