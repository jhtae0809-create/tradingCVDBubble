"""
scripts/demo_dataset.py — export / load the bundled demo dataset.

WHY THIS EXISTS
    The dashboard reads everything from a local MongoDB that is normally filled
    by the live IBKR collector. A fresh clone therefore has an empty database,
    and without an IBKR account (plus a market-data subscription) and a FinViz
    Elite token there is no way to fill it — the chart would just be blank.

    This script bundles a small, real slice of the project's own data so anyone
    can clone the repo, load it, and see the full dashboard working offline:
    candles, real tick-classified CVD, the Level-2 heatmap and the S&R lines.

WHAT IS IN THE SLICE (see demo_data/manifest.json after an export)
    ticker NVDA only, and:
      1day  / 30min  — the full stored history (long-range chart views)
      1min           — one month around the demo day
      1sec           — the demo day itself, real tick-classified bars
      level2         — the demo day's order-book snapshots, time-subsampled

    The demo day is 2026-07-22: the day with the best simultaneous coverage of
    real tick data (source='ibkr_tick') and real depth (src='ibkr').

USAGE
    # Load the bundled data into a local MongoDB (what a grader runs):
    python -m scripts.demo_dataset load

    # Re-create the bundle from a populated database (what the author runs):
    python -m scripts.demo_dataset export

Documents are stored as gzipped MongoDB Extended JSON (one document per line),
so datetimes round-trip exactly instead of degrading to strings.
"""

import argparse
import gzip
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from bson import json_util
from pymongo import MongoClient, UpdateOne

# ── What the bundle contains ─────────────────────────────────────────────────
DEMO_TICKER = "NVDA"
DEMO_DAY = datetime(2026, 7, 22)          # best tick + depth coverage
MIN_1MIN_DAYS_BEFORE = 21                 # 1min context around the demo day
MAX_1MIN_DAYS_AFTER = 10
DEFAULT_L2_EVERY = 12                     # keep every Nth depth snapshot

CANDLE_DB, CANDLE_COL = "finviz_db", "candles"
L2_DB, L2_COL = "trading_cvd", "level2_snapshots"

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "demo_data"
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")


def _write_jsonl_gz(path: Path, docs) -> tuple[int, int]:
    """Write documents as gzipped Extended-JSON lines. Returns (count, bytes)."""
    n = 0
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for doc in docs:
            doc.pop("_id", None)           # let Mongo assign fresh ids on load
            fh.write(json_util.dumps(doc) + "\n")
            n += 1
    return n, path.stat().st_size


def _read_jsonl_gz(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json_util.loads(line)


# ── export ───────────────────────────────────────────────────────────────────
def export(l2_every: int) -> None:
    client = MongoClient(MONGO_URI)
    candles = client[CANDLE_DB][CANDLE_COL]
    l2 = client[L2_DB][L2_COL]

    DATA_DIR.mkdir(exist_ok=True)
    day_start = DEMO_DAY
    day_end = DEMO_DAY + timedelta(days=1)
    manifest: dict = {
        "ticker": DEMO_TICKER,
        "demo_day": DEMO_DAY.strftime("%Y-%m-%d"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "files": {},
    }

    # Candle tiers. 1day/30min are small enough to ship whole; 1min gets a
    # window around the demo day; 1sec is the demo day only (it is by far the
    # largest tier — a single session is ~40k bars).
    tier_queries = {
        "1day":  {"ticker": DEMO_TICKER, "timeframe": "1day"},
        "30min": {"ticker": DEMO_TICKER, "timeframe": "30min"},
        "1min":  {"ticker": DEMO_TICKER, "timeframe": "1min",
                  "date": {"$gte": DEMO_DAY - timedelta(days=MIN_1MIN_DAYS_BEFORE),
                           "$lt":  DEMO_DAY + timedelta(days=MAX_1MIN_DAYS_AFTER)}},
        "1sec":  {"ticker": DEMO_TICKER, "timeframe": "1sec",
                  "date": {"$gte": day_start, "$lt": day_end}},
    }
    for tier, query in tier_queries.items():
        path = DATA_DIR / f"candles_{tier}.jsonl.gz"
        n, size = _write_jsonl_gz(path, candles.find(query).sort("date", 1))
        manifest["files"][path.name] = {"collection": f"{CANDLE_DB}.{CANDLE_COL}",
                                        "timeframe": tier, "docs": n, "bytes": size}
        print(f"  {path.name:26s} {n:>8,} docs  {size/1e6:>6.2f} MB")

    # Depth snapshots: real IBKR book on the demo day, keeping every Nth
    # snapshot. The raw 0.5s cadence would dominate the bundle, and the heatmap
    # aggregates over the visible window anyway, so a coarser cadence looks the
    # same on screen.
    cursor = l2.find({"ticker": DEMO_TICKER, "src": "ibkr",
                      "date": {"$gte": day_start, "$lt": day_end}}).sort("timestamp", 1)
    path = DATA_DIR / "level2_snapshots.jsonl.gz"
    n, size = _write_jsonl_gz(path, (d for i, d in enumerate(cursor) if i % l2_every == 0))
    manifest["files"][path.name] = {"collection": f"{L2_DB}.{L2_COL}",
                                    "docs": n, "bytes": size, "subsample_every": l2_every}
    print(f"  {path.name:26s} {n:>8,} docs  {size/1e6:>6.2f} MB")

    (DATA_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    total = sum(f["bytes"] for f in manifest["files"].values())
    print(f"\nWrote {DATA_DIR} — {total/1e6:.1f} MB total (compressed).")


# ── load ─────────────────────────────────────────────────────────────────────
def load() -> None:
    manifest_path = DATA_DIR / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"No demo data found at {DATA_DIR}. "
                         f"Run `python -m scripts.demo_dataset export` on a machine "
                         f"with a populated database first.")
    manifest = json.loads(manifest_path.read_text())

    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    try:
        client.admin.command("ping")
    except Exception as exc:                      # noqa: BLE001 - report and stop
        raise SystemExit(f"Cannot reach MongoDB at {MONGO_URI}: {exc}\n"
                         f"Start it first (macOS: `brew services start mongodb-community`).")

    # Same unique index the live pipeline relies on. Without it, upserts do a
    # full collection scan and this load takes minutes instead of seconds.
    from history.schema import ensure_indexes
    ensure_indexes(client)

    for name, meta in manifest["files"].items():
        path = DATA_DIR / name
        if not path.exists():
            print(f"  ! missing {name}, skipping")
            continue
        db_name, col_name = meta["collection"].split(".")
        col = client[db_name][col_name]

        ops, written = [], 0
        for doc in _read_jsonl_gz(path):
            if col_name == "candles":
                key = {"ticker": doc["ticker"], "timeframe": doc["timeframe"], "date": doc["date"]}
            else:
                key = {"ticker": doc["ticker"], "timestamp": doc["timestamp"], "src": doc.get("src")}
            ops.append(UpdateOne(key, {"$set": doc}, upsert=True))
            if len(ops) >= 2000:                  # bounded memory, bulk round-trips
                col.bulk_write(ops, ordered=False)
                written += len(ops)
                ops = []
        if ops:
            col.bulk_write(ops, ordered=False)
            written += len(ops)
        print(f"  {name:26s} -> {meta['collection']:32s} {written:>8,} docs")

    print(f"\nLoaded the {manifest['ticker']} demo dataset (demo day {manifest['demo_day']}).")
    print("Next:")
    print("  1. ./start_all.sh          (or: python -m app  — the collector is not needed)")
    print("  2. open http://127.0.0.1:8050")
    print(f"  3. search {manifest['ticker']}, then use 'Jump to (ET)' -> {manifest['demo_day']} 10:00")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("load", help="load demo_data/ into the local MongoDB")
    exp = sub.add_parser("export", help="rebuild demo_data/ from a populated MongoDB")
    exp.add_argument("--l2-every", type=int, default=DEFAULT_L2_EVERY,
                     help=f"keep every Nth depth snapshot (default {DEFAULT_L2_EVERY})")
    args = parser.parse_args()

    if args.cmd == "export":
        export(args.l2_every)
    else:
        load()


if __name__ == "__main__":
    main()
