"""
ibkr/reclassify.py — offline, time-aligned trade re-classification (quote-lag fix)
---------------------------------------------------------------------------------
The real-time collector classifies each trade against whatever NBBO snapshot it
happens to hold, which can lag the trade by hundreds of ms (AllLast and BidAsk
are independent streams). Empirically stale quotes bias the aggressor label
toward SELL, which drags CVD away from price.

The industry-standard fix (Lee-Ready with proper quote alignment) is to match
each trade to the quote that was PREVAILING at the trade's own timestamp. With
the full NBBO stream persisted to `raw_quotes` (see tick_collector.py) we can do
this exactly with a backward merge_asof, then re-classify with the shared
vectorized aggressor.

This is strictly better than the load-time stale-quote demotion in
cvd/calculator.py (load_from_mongo(..., reclassify_stale_ms=...)), which can only
fall back to the tick rule because it lacks the intermediate quotes. Use that as
a stopgap on already-collected ticks; use THIS once raw_quotes has data.

Usage:
    python -m ibkr.reclassify --ticker NVDA                 # dry-run report
    python -m ibkr.reclassify --ticker NVDA --tolerance-ms 200
    python -m ibkr.reclassify --ticker NVDA --write         # rewrite raw_ticks.delta + rebuild 1sec bars
"""
import argparse
import os

import numpy as np
import pandas as pd
from pymongo import MongoClient

from cvd.aggressor import classify_vectorized

# Overridable so the app can run against a MongoDB that is not on this
# machine. Railway (and any container deploy) runs the database as a
# separate service, where "localhost" is the app container itself and
# resolves to nothing. Default unchanged, so local runs are unaffected.
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME = "finviz_db"


def load_streams(ticker: str):
    """Return (trades_df, quotes_df) sorted by time. Empty quotes -> no raw_quotes yet."""
    c = MongoClient(MONGO_URI)[DB_NAME]
    trades = pd.DataFrame(list(c["raw_ticks"].find(
        {"ticker": ticker}, {"_id": 1, "date": 1, "price": 1, "size": 1, "delta": 1, "source": 1})))
    quotes = pd.DataFrame(list(c["raw_quotes"].find(
        {"ticker": ticker}, {"_id": 0, "date": 1, "bid": 1, "ask": 1})))
    if not trades.empty:
        trades = trades.sort_values("date").reset_index(drop=True)
    if not quotes.empty:
        quotes = quotes.sort_values("date").reset_index(drop=True)
    return trades, quotes


def reclassify(trades: pd.DataFrame, quotes: pd.DataFrame,
               tolerance_ms: float | None = None) -> pd.DataFrame:
    """Attach the prevailing NBBO to each trade (backward merge_asof) and
    re-classify. Returns trades with a new `delta_new` column.

    tolerance_ms: if set, a trade with no quote within this window gets NaN
                  bid/ask -> classify_vectorized falls back to the tick rule.
    """
    tol = pd.Timedelta(milliseconds=tolerance_ms) if tolerance_ms else None
    merged = pd.merge_asof(
        trades, quotes, on="date", direction="backward",
        tolerance=tol,
    )
    delta, _, _ = classify_vectorized(
        merged["price"].to_numpy(float), merged["size"].to_numpy(float),
        merged["bid"].to_numpy(float), merged["ask"].to_numpy(float),
    )
    merged["delta_new"] = delta
    return merged


def _tracking(df: pd.DataFrame, delta_col: str, rule: str = "30s") -> tuple[float, float]:
    """(level corr, direction-agreement %) of cumulative delta vs price, reg hours."""
    d = df.set_index("date")
    m = d.index.hour * 60 + d.index.minute
    d = d[(m >= 570) & (m < 960)]
    if len(d) < 20:
        return float("nan"), float("nan")
    g = d.resample(rule).agg(price=("price", "last"), delta=(delta_col, "sum")).dropna()
    g["cvd"] = g["delta"].cumsum()
    dP = g["price"].diff()
    dd = pd.DataFrame({"dP": dP, "de": g["delta"]}).dropna()
    dd = dd[(dd["dP"] != 0) & (dd["de"] != 0)]
    agree = (np.sign(dd["dP"]) == np.sign(dd["de"])).mean() * 100 if len(dd) else float("nan")
    return g["price"].corr(g["cvd"]), agree


def rebuild_1sec(trades: pd.DataFrame, ticker: str, write: bool):
    """Rebuild the 1sec candle deltas from the re-classified trades (delta_new)."""
    d = trades.set_index("date")
    bar = d.resample("1s").agg(
        open=("price", "first"), high=("price", "max"), low=("price", "min"),
        close=("price", "last"), volume=("size", "sum"),
        buying_volume=("delta_new", lambda s: s[s > 0].sum()),
        selling_volume=("delta_new", lambda s: -s[s < 0].sum()),
    ).dropna(subset=["open"])
    bar["delta"] = bar["buying_volume"] - bar["selling_volume"]
    if write:
        col = MongoClient(MONGO_URI)[DB_NAME]["candles"]
        for ts, row in bar.iterrows():
            col.update_one(
                {"ticker": ticker, "timeframe": "1sec", "date": ts.to_pydatetime()},
                {"$set": {
                    "buying_volume": float(row["buying_volume"]),
                    "selling_volume": float(row["selling_volume"]),
                    "delta": float(row["delta"]),
                    "reclassified": True,
                }},
            )
        print(f"[reclassify] rewrote {len(bar)} 1sec bars for {ticker}")
    return bar


def main():
    ap = argparse.ArgumentParser(description="Time-aligned trade re-classification (quote-lag fix)")
    ap.add_argument("--ticker", default="NVDA")
    ap.add_argument("--tolerance-ms", type=float, default=None,
                    help="Max quote age for merge_asof; older -> tick-rule fallback")
    ap.add_argument("--write", action="store_true",
                    help="Rewrite raw_ticks.delta and rebuild 1sec candle deltas")
    args = ap.parse_args()

    trades, quotes = load_streams(args.ticker)
    if trades.empty:
        print(f"No raw_ticks for {args.ticker}.")
        return
    if quotes.empty:
        print("No raw_quotes yet — restart the (updated) tick_collector to persist the "
              "NBBO stream, then re-run. Meanwhile use the stopgap in calculator.py:\n"
              "  load_from_mongo(ticker, 'raw_tick', reclassify_stale_ms=50).")
        return

    print(f"trades={len(trades)}  quotes={len(quotes)}  "
          f"{trades['date'].min()} ~ {trades['date'].max()}")

    # Guard: merge_asof silently tick-rules any trade with no prior quote, so a
    # sparse raw_quotes stream yields meaningless numbers. Require the quote
    # stream to actually cover the trades before trusting the result.
    q_lo, q_hi = quotes["date"].min(), quotes["date"].max()
    in_window = trades["date"].between(q_lo, q_hi).mean()
    print(f"quotes cover {q_lo} ~ {q_hi}  ({in_window*100:.1f}% of trades fall in-window)")
    if in_window < 0.5:
        print("\n⚠️  raw_quotes covers <50% of trades — the collector has not yet run a\n"
              "    full session with NBBO persistence. Results below are NOT reliable.\n"
              "    Collect a full session, then re-run. Stopgap meanwhile:\n"
              "    load_from_mongo(ticker, 'raw_tick', reclassify_stale_ms=50).")

    merged = reclassify(trades, quotes, args.tolerance_ms)

    lvl0, dir0 = _tracking(merged.assign(delta=merged["delta"]), "delta")
    lvl1, dir1 = _tracking(merged, "delta_new")
    print(f"\n            LEVEL   dir%")
    print(f"  stored :  {lvl0:+.3f}  {dir0:.1f}")
    print(f"  aligned:  {lvl1:+.3f}  {dir1:.1f}   (merge_asof, tol={args.tolerance_ms}ms)")
    print(f"  net delta: stored {merged['delta'].sum():+,.0f} -> aligned {merged['delta_new'].sum():+,.0f}")

    if args.write:
        col = MongoClient(MONGO_URI)[DB_NAME]["raw_ticks"]
        for _id, nd in zip(merged["_id"], merged["delta_new"]):
            col.update_one({"_id": _id}, {"$set": {"delta": float(nd), "reclassified": True}})
        print(f"[reclassify] rewrote {len(merged)} raw_ticks deltas for {args.ticker}")
        rebuild_1sec(merged, args.ticker, write=True)


if __name__ == "__main__":
    main()
