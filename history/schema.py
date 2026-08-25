"""
history/schema.py
-----------------
Canonical resolution-ladder ("tier") definitions and serving map.

Design (see Claude/{CVD Trading} History Pipeline Plan.md):
  Each chart timeframe is served from the nearest materialized tier at or
  below it — never resampled from ticks. Tiers live in the existing
  finviz_db.candles collection, distinguished by the `timeframe` field, and
  carry write-time-computed buying_volume / selling_volume / delta plus a
  `quality` flag describing how trustworthy the buy/sell split is.

Quality ladder (higher rank must never be overwritten by lower):
  tick    — real aggressor classification from live tick+quote streams
  mixed   — aggregated bucket containing both tick and estimated sub-bars
  bvc     — Bulk Volume Classification estimate (bar data only)
  wick    — legacy wick-decomposition estimate
  neutral — direction unknowable (auction prints etc.), split 50/50
"""

from datetime import time

import os

from pymongo import MongoClient

# Overridable so the app can run against a MongoDB that is not on this
# machine. Railway (and any container deploy) runs the database as a
# separate service, where "localhost" is the app container itself and
# resolves to nothing. Default unchanged, so local runs are unaffected.
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME = "finviz_db"

# ── Materialized tiers ────────────────────────────────────────────────────────
# tf label in candles → (pandas resample rule, hot retention in days; None = keep forever)
TIERS = {
    "1sec":  {"rule": "1s",    "retention_days": 30},
    "1min":  {"rule": "1min",  "retention_days": 180},
    "30min": {"rule": "30min", "retention_days": 730},
    "1day":  {"rule": "1D",    "retention_days": None},
}

# Rollup chain: src tier → dst tier (run in this order).
ROLLUP_CHAIN = [("1sec", "1min"), ("1min", "30min"), ("30min", "1day")]

# ── Serving map: chart timeframe → tier it is loaded/resampled from ──────────
SERVE_TIER = {
    "1sec":   "1sec",
    "5sec":   "1sec",
    "1min":   "1min",
    "3min":   "1min",
    "5min":   "1min",
    "15min":  "1min",
    "1hr":    "30min",
    "3hr":    "30min",
    "1day":   "1day",
    "1week":  "1day",
    "1month": "1day",
}

# Finer-tier fallback order when the mapped tier has no data yet.
TIER_FALLBACK = {
    "1day":  ["30min", "1min", "1sec"],
    "30min": ["1min", "1sec"],
    "1min":  ["1sec"],
    "1sec":  [],
}

# Default lookback window (calendar days) per chart timeframe. None = everything.
# Sized to ~3,000 bars: the chart RENDERS only 1,000 by default (visualizer
# MAX_CANDLES) and grows progressively on pan/zoom-out (app bars-to-show), so
# pre-loading a few growth steps' worth makes the first expansions instant —
# loading is ~0.1s while rendering is the real cost. Deeper panning grows the
# window itself through the days-to-load store.
SERVE_WINDOW_DAYS = {
    "1sec": 1, "5sec": 1,
    "1min": 5, "3min": 15, "5min": 25, "15min": 70,
    "1hr": 400, "3hr": 730,
    "1day": None, "1week": None, "1month": None,
}

# ── Quality ranks ─────────────────────────────────────────────────────────────
QUALITY_RANK = {"tick": 4, "mixed": 3, "bvc": 2, "wick": 1, "neutral": 0}

SOURCE_DEFAULT_QUALITY = {
    "ibkr_tick": "tick",
    "alpaca_tick": "tick",
    "ibkr_hist": "bvc",     # OHLCV-only bars → estimated at write time
    "finviz": "bvc",
    "finviz_wick": "wick",  # legacy docs
}


def quality_of(source: str) -> str:
    return SOURCE_DEFAULT_QUALITY.get(source, "bvc")


def worst_quality(qualities) -> str:
    """Aggregate quality for a rolled-up bucket: tick-only stays tick,
    estimate-only stays that estimate, and a mix of ranks becomes 'mixed'."""
    uniq = {q for q in qualities if isinstance(q, str)}
    if not uniq:
        return "bvc"
    if len(uniq) == 1:
        return uniq.pop()
    if "tick" in uniq:
        return "mixed"
    return min(uniq, key=lambda q: QUALITY_RANK.get(q, 0))


# ── Trading sessions (ET wall clock, matches tick_collector storage) ──────────
SESSION_START = time(4, 0)     # pre-market open
SESSION_END = time(20, 0)      # after-hours close (grid end, exclusive)

# Timeframes that get the expected-session grid (empty candles for missing bars).
GRID_TIMEFRAMES = {"1min", "3min", "5min", "15min", "1hr", "3hr"}


def mongo_client() -> MongoClient:
    return MongoClient(MONGO_URI)


def candles_col(client: MongoClient | None = None):
    client = client or mongo_client()
    return client[DB_NAME]["candles"]


def ensure_indexes(client: MongoClient | None = None) -> None:
    """Create the compound unique index (idempotent) and meta collections."""
    client = client or mongo_client()
    db = client[DB_NAME]
    db["candles"].create_index(
        [("ticker", 1), ("timeframe", 1), ("date", 1)],
        unique=True, name="ticker_tf_date", background=True,
    )
    db["rollup_meta"].create_index(
        [("ticker", 1), ("src", 1), ("dst", 1)], unique=True, background=True,
    )
    db["backfill_meta"].create_index(
        [("ticker", 1), ("timeframe", 1)], unique=True, background=True,
    )
    # raw_ticks / raw_quotes are queried by {ticker, date} — calculator.py for
    # the raw_tick timeframe, prune.py, reclassify.py. These indexes existed on
    # the development machine only because someone built them by hand years ago;
    # nothing created them, so every other machine ran those queries as
    # collection scans that grow with the tick history until the chart stops
    # responding. Not unique: duplicate prints at the same timestamp are real.
    for raw in ("raw_ticks", "raw_quotes"):
        db[raw].create_index([("ticker", 1), ("date", 1)], background=True)
