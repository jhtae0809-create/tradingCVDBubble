import pandas as pd
import numpy as np
from pymongo import MongoClient
import os
import time
import math

# L2 heatmap / support-resistance price band. Resting orders parked far from the
# current price (e.g. a lone limit at 225 while the stock is 207) are real but
# irrelevant as S&R, and they stretch the heatmap y-axis so the candles get
# squished. Levels beyond L2_BAND from the latest close are dropped from the
# heatmap grid (and therefore from the S&R detector and the y-fit).
L2_BAND = float(os.environ.get("L2_BAND", "0.05"))

# Overridable so the app can run against a MongoDB that is not on this
# machine. Railway (and any container deploy) runs the database as a
# separate service, where "localhost" is the app container itself and
# resolves to nothing. Default unchanged, so local runs are unaffected.
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME = "trading_cvd"
COLLECTION_NAME = "level2_snapshots"

def get_l2_collection():
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    return db[COLLECTION_NAME]


# ── Snapshot document schema (shared by ibkr/level2_collector.py and
#    tests/mock_level2_stream.py so mock and real data are interchangeable) ──

def et_epoch(et_naive_dt) -> float:
    """Epoch for an ET-naive datetime using the same convention as the candle
    side: candles store ET-naive dates and fetch_and_aggregate_l2_data calls
    `.timestamp()` on them, which treats naive as UTC — so snapshot epochs
    must be produced the same way or every match is offset by the UTC gap."""
    return pd.Timestamp(et_naive_dt).timestamp()


def snapshot_doc(ticker: str, et_dt, bids: list, asks: list, src: str) -> dict:
    """bids/asks: [{"price": float, "size": float}, ...] best-first."""
    mid = (bids[0]["price"] + asks[0]["price"]) / 2.0 if bids and asks else None
    return {
        "ticker": ticker.upper(),
        "timestamp": et_epoch(et_dt),
        "date": pd.Timestamp(et_dt).to_pydatetime(),   # ET naive, for humans/queries
        "bids": bids,
        "asks": asks,
        "mid_price": mid,
        "src": src,                                    # 'ibkr' | 'mock'
    }


def ensure_l2_indexes(col=None) -> None:
    col = col if col is not None else get_l2_collection()
    col.create_index([("ticker", 1), ("timestamp", 1)], background=True)


def compute_support_resistance(y_levels, z_matrix, mid_price,
                               max_each: int = 2,
                               min_persistence: float = 0.3,
                               score_mult: float = 2.5,
                               keep2_frac: float = 0.5,
                               z_bid=None) -> list:
    """Persistent large resting-liquidity levels → support/resistance lines.

    score(level) = (average resting size while present) × (fraction of
    snapshots where the level had size). Requiring persistence filters out
    one-shot icebergs/spoofs that light up a single snapshot; requiring
    score_mult× the median positive score filters ordinary book depth.
    Adjacent qualifying levels (within 2 price steps) merge into one line at
    the strongest level. Returns up to max_each per side:
    [{"price", "score" (0-1 vs strongest), "side": "support"|"resistance"}].

    Side: with z_bid (bid-only sizes, same shape as z_matrix) a wall is
    classified by WHICH SIDE its liquidity sat on — bid-dominant = support,
    ask-dominant = resistance. That is the semantic definition and stays
    correct even when the last close drifts away from the book (the price-
    vs-mid fallback then labels everything one side).
    """
    if z_matrix is None or y_levels is None or len(y_levels) == 0 or mid_price is None:
        return []
    z = np.asarray(z_matrix, dtype=float)
    if z.ndim != 2 or z.shape[1] == 0:
        return []
    present = z > 0
    # Persistence relative to columns that HAVE a book — candles outside the
    # collector's coverage (all-zero columns) must not dilute the fraction.
    n_covered = int(present.any(axis=0).sum())
    if n_covered == 0:
        return []
    persistence = present.sum(axis=1) / n_covered
    n_present = np.maximum(present.sum(axis=1), 1)
    avg_size = np.where(present.any(axis=1), z.sum(axis=1) / n_present, 0.0)
    score = avg_size * persistence

    pos = score[score > 0]
    if pos.size == 0:
        return []
    med = float(np.median(pos))
    if med <= 0:
        return []
    step = float(np.median(np.diff(y_levels))) if len(y_levels) > 1 else 0.01

    qualifying = [(float(y_levels[i]), float(score[i]), float(avg_size[i]))
                  for i in range(len(y_levels))
                  if score[i] >= score_mult * med and persistence[i] >= min_persistence]
    if not qualifying:
        return []

    # Merge adjacent runs (y_levels is sorted ascending), keep each run's peak.
    merged, run_peak, last_p = [], None, None
    for p, s, sz in qualifying:
        if last_p is not None and (p - last_p) <= 2 * step:
            if s > run_peak[1]:
                run_peak = (p, s, sz)
        else:
            if run_peak is not None:
                merged.append(run_peak)
            run_peak = (p, s, sz)
        last_p = p
    merged.append(run_peak)

    s_max = max(s for _, s, _ in merged)

    if z_bid is not None:
        zb = np.asarray(z_bid, dtype=float)
        level_idx = {float(p): i for i, p in enumerate(y_levels)}

        def _is_support(price):
            i = level_idx[price]
            tot = z[i].sum()
            return tot > 0 and (zb[i].sum() / tot) >= 0.5
    else:
        def _is_support(price):
            return price < mid_price

    # Per side: always keep the single strongest wall; keep the 2nd only when
    # it is at least keep2_frac as strong as the 1st. This avoids the cluttered
    # "2-3 lines a side" look — a weak secondary wall no longer draws a line
    # (and no longer visually competes with the real level).
    def _pick(side_levels):
        ranked = sorted(side_levels, key=lambda t: -t[1])[:max_each]
        if len(ranked) >= 2 and ranked[1][1] < keep2_frac * ranked[0][1]:
            ranked = ranked[:1]
        return ranked
    supports    = _pick([m for m in merged if _is_support(m[0])])
    resistances = _pick([m for m in merged if not _is_support(m[0])])
    # "size" = average resting size while the level was present (shares) —
    # shown in the chart label so a wall's actual thickness is readable.
    return ([{"price": p, "score": s / s_max, "side": "support", "size": sz}
             for p, s, sz in supports] +
            [{"price": p, "score": s / s_max, "side": "resistance", "size": sz}
             for p, s, sz in resistances])

def calculate_center_of_gravity(orders):
    """
    Calculates the volume-weighted average price (Center of Gravity)
    for a list of orders [{"price": float, "size": float}, ...].
    """
    if not orders:
        return np.nan
    total_volume = sum(o['size'] for o in orders)
    if total_volume == 0:
        return np.nan
    weighted_price = sum(o['price'] * o['size'] for o in orders)
    return weighted_price / total_volume

def calculate_obi(bids, asks):
    """
    Order Book Imbalance (OBI) Ratio
    (Total Bid Volume - Total Ask Volume) / (Total Bid Volume + Total Ask Volume)
    """
    total_bid_vol = sum(b['size'] for b in bids)
    total_ask_vol = sum(a['size'] for a in asks)
    total_vol = total_bid_vol + total_ask_vol
    if total_vol == 0:
        return 0.0
    return (total_bid_vol - total_ask_vol) / total_vol

def calculate_weighted_liquidity(orders, decay=3.0):
    """
    Calculates weighted liquidity using exponential decay.
    orders: list of dicts sorted from best to worst.
    """
    weighted_vol = 0.0
    for level, o in enumerate(orders):
        weighted_vol += o['size'] * math.exp(-level / decay)
    return weighted_vol

def fetch_and_aggregate_l2_data(ticker, df_candles, max_candles=300):
    """
    Matches L2 snapshots to the provided candlestick dataframe.
    Returns the dataframe enriched with L2 metrics and the Z-matrix for the heatmap.
    df_candles index should be a datetime index.
    """
    col = get_l2_collection()
    
    # We only process the last `max_candles` to save performance
    if len(df_candles) > max_candles:
        df_subset = df_candles.iloc[-max_candles:].copy()
    else:
        df_subset = df_candles.copy()
        
    df_candles = df_candles.copy()
    for c in ['obi', 'bid_weighted_liq', 'ask_weighted_liq', 'bid_cog', 'ask_cog']:
        df_candles[c] = np.nan
        
    if df_subset.empty:
        return df_candles, None, None, None
        
    start_ts = df_subset.index[0].timestamp()
    end_ts = df_subset.index[-1].timestamp()

    # Only ONE snapshot per candle survives the backward merge_asof below, but
    # the raw stream holds ~2 snapshots/second — a naive range find() drags in
    # tens of thousands of full-book docs to use <1% of them (measured: 46,010
    # fetched / 300 used, ~3s of BSON decode per refresh — the "L2 toggle is
    # slow" symptom). Two-phase fetch instead:
    #   1. covered query for timestamps only (cheap: floats off the index),
    #   2. pick the backward-nearest ts per candle in numpy,
    #   3. $in-fetch just those ≤ n_candles full-book docs.
    # Measured 1.45s → 0.09s for the same window; z-matrix identical.
    buffer = 120.0   # matches the merge_asof tolerance below
    ts_arr = np.array(sorted(
        d["timestamp"] for d in col.find(
            {"ticker": ticker.upper(),
             "timestamp": {"$gte": start_ts - buffer, "$lte": end_ts}},
            {"timestamp": 1, "_id": 0})
    ), dtype=float)
    if ts_arr.size == 0:
        return df_candles, None, None, None
    cand_ts = np.array([ts.timestamp() for ts in df_subset.index], dtype=float)
    nearest = np.searchsorted(ts_arr, cand_ts, side="right") - 1   # backward, inclusive
    chosen = sorted({float(ts_arr[i]) for i in nearest if i >= 0})
    snapshots = list(col.find(
        {"ticker": ticker.upper(), "timestamp": {"$in": chosen}},
        {"_id": 0, "timestamp": 1, "bids": 1, "asks": 1}))

    if not snapshots:
        return df_candles, None, None, None

    # Pre-process snapshots into a DataFrame for efficient mapping
    snap_df = pd.DataFrame({
        'timestamp': [s['timestamp'] for s in snapshots],
        'bids': [s['bids'] for s in snapshots],
        'asks': [s['asks'] for s in snapshots]
    })
    
    # For each candle, find the closest snapshot AT OR BEFORE the candle's timestamp.
    # We use merge_asof for efficient matching.
    # We need candle timestamps as a column.
    df_subset['candle_ts'] = df_subset.index.map(lambda x: x.timestamp())
    
    # Ensure snap_df is sorted by timestamp
    snap_df = snap_df.sort_values('timestamp')
    
    # Merge asof. The tolerance matters: without it a candle HOURS after the
    # last snapshot still matches it (stale ghost book carried forward when
    # the collector was down), which inflates persistence and fabricates S&R
    # levels at long-gone prices.
    merged = pd.merge_asof(
        df_subset.reset_index(),
        snap_df,
        left_on='candle_ts',
        right_on='timestamp',
        direction='backward',
        tolerance=120.0
    )
    
    # Calculate metrics row by row
    obi_list = []
    bid_wl_list = []
    ask_wl_list = []
    bid_cog_list = []
    ask_cog_list = []
    
    # For Heatmap
    # We will build a unified price scale (y_levels) across all snapshots in the subset
    min_price = float('inf')
    max_price = float('-inf')
    
    for _, row in merged.iterrows():
        bids = row['bids'] if isinstance(row['bids'], list) else []
        asks = row['asks'] if isinstance(row['asks'], list) else []
        
        obi_list.append(calculate_obi(bids, asks))
        bid_wl_list.append(calculate_weighted_liquidity(bids))
        ask_wl_list.append(calculate_weighted_liquidity(asks))
        bid_cog_list.append(calculate_center_of_gravity(bids))
        ask_cog_list.append(calculate_center_of_gravity(asks))
        
    # Use iloc for assignment to avoid "cannot reindex from a duplicate axis" errors
    # which happen when df_candles has duplicate timestamps (e.g. in Raw Ticks data).
    N = len(obi_list)
    df_candles.iloc[-N:, df_candles.columns.get_loc('obi')] = obi_list
    df_candles.iloc[-N:, df_candles.columns.get_loc('bid_weighted_liq')] = bid_wl_list
    df_candles.iloc[-N:, df_candles.columns.get_loc('ask_weighted_liq')] = ask_wl_list
    df_candles.iloc[-N:, df_candles.columns.get_loc('bid_cog')] = bid_cog_list
    df_candles.iloc[-N:, df_candles.columns.get_loc('ask_cog')] = ask_cog_list
    
    # Build Heatmap Z-Matrix
    all_prices = set()
    for _, row in merged.iterrows():
        bids = row['bids'] if isinstance(row['bids'], list) else []
        asks = row['asks'] if isinstance(row['asks'], list) else []
        for b in bids:
            all_prices.add(b['price'])
        for a in asks:
            all_prices.add(a['price'])
            
    y_levels = None
    z_matrix = None
    z_bid = None

    if all_prices:
        y_levels = sorted(list(all_prices))
        price_to_idx = {p: i for i, p in enumerate(y_levels)}

        # z_matrix shape: (len(y_levels), len(df_subset)); z_bid keeps the
        # bid-side share so S&R can classify a wall by WHICH SIDE the resting
        # size sat on (bid wall = support, ask wall = resistance) instead of
        # by price-vs-last-close, which flips when price runs away from a
        # stale book.
        z_matrix = np.zeros((len(y_levels), len(df_subset)))
        z_bid = np.zeros_like(z_matrix)

        for col_idx, (_, row) in enumerate(merged.iterrows()):
            bids = row['bids'] if isinstance(row['bids'], list) else []
            asks = row['asks'] if isinstance(row['asks'], list) else []
            for b in bids:
                row_idx = price_to_idx[b['price']]
                z_matrix[row_idx, col_idx] += b['size']
                z_bid[row_idx, col_idx] += b['size']
            for a in asks:
                row_idx = price_to_idx[a['price']]
                z_matrix[row_idx, col_idx] += a['size']

        # Clip levels parked far from the current price so a lone limit order
        # (e.g. 225 while price is 207) can't stretch the heatmap axis or invent
        # an S&R line. Band is relative to the latest candle close.
        try:
            ref = float(df_candles["close"].iloc[-1])
        except Exception:
            ref = None
        if ref and ref > 0 and len(y_levels) > 0:
            lo, hi = ref * (1 - L2_BAND), ref * (1 + L2_BAND)
            keep = [i for i, p in enumerate(y_levels) if lo <= p <= hi]
            if keep and len(keep) < len(y_levels):
                y_levels = [y_levels[i] for i in keep]
                z_matrix = z_matrix[keep, :]
                z_bid = z_bid[keep, :]

    return df_candles, y_levels, z_matrix, z_bid
