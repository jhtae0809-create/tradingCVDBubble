"""Buy/Sell Volume Decomposition + CVD Calculator

Pipeline:
    source='ibkr_tick'
        IBKR real-time tick (0.1s) data; buying_volume/selling_volume/delta
        pre-computed by tick_collector.py (quote-based aggressor).
        Wick decomposition is SKIPPED.

    source='ibkr_tick'
        IBKR real-time/historical tick data; buying_volume/selling_volume/delta
        pre-computed by tick_collector.py / backfill.py. 
        Wick decomposition is SKIPPED.

    source='ibkr_hist'
        1-sec bars from reqHistoricalData (no tick-level quotes).
        Wick decomposition is APPLIED (same as FinViz).
    
    source='finviz_wick'
        FinViz Elite 1-min bars. 
        Wick decomposition is APPLIED.

This file can also be imported as a module and contains the following
functions:

    * decompose_candle - Calculates the buy/sell volume & change in
      price given the OHLCV.

add_cvd_columns() detects which rows have pre-computed values and branches
accordingly, so mixed DataFrames (ibkr_tick + finviz_wick) work
transparently.
"""

import numpy as np
import pandas as pd
from pymongo import MongoClient
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


# ------------------------------------------
# 1. Extract Buy/Sell Volume From One Candle
# ------------------------------------------

def decompose_candle(o: float, h: float, l: float, c: float, v: float) -> dict:
    """Return estimated buy/sell volume from one candle (OHLCV).

    Variables:
       * spread = high - low
       * half_wicks =  (upper_wick + lower_wick) / 2
       * body = spread - upper_wick - lower_wick

    Parameters
    ----------
    o : float
        Open   (price at start of time-interval)
    h : float
        High   (highest price during time-interval)
    l : float
        Low    (lowest price during time-interval)
    c : float
        Close  (price at end of time-interval)
    v : float
        Volume (total volume traded during tine-interval period)

    Returns
    -------
    { buying_volume, selling_volume, delta } : dict
    buying_volume : float
        Approximates "num. of buys" by taking avg. of wicks 
        (+ half_wicks if BULLISH)
    selling_volume : float
        Approximates "num. of sells" by taking avg. of wicks 
        (+ half_wicks if BEARISH)
    delta : float
        buying_volume - selling_volume

    Restrictions
    ------------
    Time = Closing auction (15:59 print):
        US exchanges settle all Market-On-Close orders in a single closing
        cross at one price, so the 15:59 bar carries a huge volume (often ~40x
        a normal minute and ~2/3 of the final hour) packed into a near-zero
        range / doji. 
        
        This wick model then decides direction from a 1-2 cent
        open/close difference and dumps almost the entire print onto one side,
        so the buy/sell SIGN of that bar flips day to day and is unreliable
        (e.g. a green buy spike on a down day). 
        
        The volume itself is real; only
        its buy/sell split is meaningless for a single-price auction print.
        Per design decision (6/25) the logic is left as-is and documented here.
    """
    spread = h - l

    # spread=0 -> unable to calculate, assume buying volume == selling volume
    if spread == 0:
        return {"buying_volume": v / 2, "selling_volume": v / 2, "delta": 0.0}

    # ── Wick calculation (direction-aware)
    if c > o:  # Bullish
        upper_wick = h - c
        lower_wick = o - l
    else:      # Bearish
        upper_wick = h - o
        lower_wick = c - l

    body = spread - upper_wick - lower_wick

    # ── Convert each component to a percentage of the spread
    pct_upper = upper_wick / spread
    pct_lower = lower_wick / spread
    pct_body  = body / spread

    # Wicks are contested zones -> split equally between buyers and sellers
    half_wicks = (pct_upper + pct_lower) / 2

    # ── Volume distribution
    if c > o:   # Bullish: buyers take the body + half of the wicks
        buying_volume  = (pct_body + half_wicks) * v
        selling_volume = half_wicks * v
    else:       # Bearish: sellers take the body + half of the wicks
        buying_volume  = half_wicks * v
        selling_volume = (pct_body + half_wicks) * v

    delta = buying_volume - selling_volume

    return {
        "buying_volume":  buying_volume,
        "selling_volume": selling_volume,
        "delta":          delta,        
    }


# ---------------------------
# 2. Detect "Auction" Anomaly
# ---------------------------

def _flag_auction(df: pd.DataFrame, mult: float = 6.0, spill_mult: float = 3.0) -> pd.Series:
    """Boolean Series marking each day's closing-cross bars.

    The closing auction is one batch print (Market-On-Close cross) that the
    consolidated tape stamps with a sale-condition code (6 / M / X). We do not
    have those codes in backfilled / FinViz data, so we detect the cross by its
    volume signature — but the signature differs by granularity:

    * SUB-MINUTE (1-sec bars present): the cross lands in a single second that
      dwarfs the day. We flag any bar inside the fixed closing window whose
      volume exceeds `_AUCTION_REF_MULT`x the day's regular-session (09:30-15:55)
      1-second 99th-percentile volume. Measured across names the cross second is
      44-9000x that p99 while genuine 15:59 continuous trading stays under ~7x,
      so the window separates cleanly and name-agnostically (a heavy-volume,
      low-price name like SOFI, whose cross is only ~10x its MINUTE median and so
      slips past the minute test, is caught here).
    * 1-MINUTE+ (coarse tiers, no sub-second detail): fall back to the minute
      test — the closing-window minute must dwarf the day's 09:30-13:00 median
      (> `mult`x) — plus a forward spill for the 16:00 overflow bar.
    """
    if len(df) >= 2:
        # PART 1: Check if average time-interval (for data) is < 1m accuracy
        spacing = df.index.to_series().diff().dt.total_seconds().median()
        if pd.notna(spacing) and spacing < 60:
            return _flag_auction_subminute(df)
    return _flag_auction_1min(df, mult, spill_mult)


# Closing-window bar counts as the auction when its volume exceeds this multiple
# of the day's regular-session 1-second p99 (see _flag_auction). 10x sits in the
# clean gap between the cross (>=44x observed) and pre-close continuous flow
# (<=7x observed).
_AUCTION_REF_MULT = 10.0

# Exchange condition codes (tokens inside AllLast `specialConditions`, stored by
# tick_collector as `special_conditions`) that mark the single-priced CLOSING
# cross — the industry-standard way to identify the auction print. These are the
# consolidated-tape sale conditions for the close:
#     '6' = Closing Prints (Market Center Closing Trade)
#     'M' = Market Center Official Close
# CONFIRMED against a real close (NVDA 2026-07-27 16:00:00, verified with
# scripts/inspect_auction_conditions.py): the closing-cross tick carried
# specialConditions '6 X,F,F I,FT,FTI,I,T,TI' — i.e. token '6' matched. In the
# same session, regular intraday flow only ever carried {I, F, 4, 7, V, W, C}
# and '6' appeared exactly once, on the cross — so it is false-positive-safe.
# ('M' was not observed but is kept as a harmless documented fallback for venues
# that stamp the official close with it; it likewise never appears intraday.)
# These are ORed with the volume heuristic in add_cvd_columns, which is still
# needed: same-second auction volume also arrives on finviz/ibkr_hist bars that
# carry NO condition string, and only the heuristic catches those.
_AUCTION_CONDITION_CODES: set[str] = {"6", "M"}


def _has_auction_condition(cond) -> bool:
    """True if a stored `special_conditions` string contains a known auction code.
    Tokens may be comma- and/or space-separated (e.g. '4 I,7 V,M')."""
    if not _AUCTION_CONDITION_CODES or not isinstance(cond, str) or not cond:
        return False
    tokens = {c.strip() for part in cond.split(",") for c in part.split() if c.strip()}
    return bool(tokens & _AUCTION_CONDITION_CODES)


def _flag_auction_subminute(df: pd.DataFrame, ref_mult: float = _AUCTION_REF_MULT) -> pd.Series:
    """Sub-minute closing-cross detection (see _flag_auction).

    Flags individual sub-minute bars inside the fixed closing window
    (15:59-16:01 normal day, 12:59-13:01 half-day) whose volume dwarfs the day's
    regular-session 1-second p99. A half-day is decided by EVIDENCE (thin
    post-13:00 trade), not by where the data ends, so a live pre-close view gets
    no early-close verdict.
    """
    flag = pd.Series(False, index=df.index)
    vol = df["volume"]
    for _, g in df.groupby(lambda ix: ix.date()):
        minute = g.index.hour * 60 + g.index.minute
        gvol = g["volume"]

        # Regular-session 1-sec reference (09:30-15:55).
        reg = gvol[(minute >= 570) & (minute <= 955)]
        if len(reg) < 100:
            continue                       # too little regular data to judge
        ref = reg.quantile(0.99)
        if not ref > 0:
            continue

        # Half-day evidence: enough thin minutes after 13:00 vs the morning.
        vmin = gvol.groupby(minute).sum()
        base = vmin[(vmin.index >= 570) & (vmin.index < 780)]
        base_med = base.median() if not base.empty else 0
        aft = vmin[(vmin.index >= 790) & (vmin.index <= 955)]
        half = base_med > 0 and len(aft) >= 5 and aft.median() < base_med * 0.1
        lo, hi = (779, 781) if half else (959, 961)   # 12:59-13:01 or 15:59-16:01

        win = gvol[(minute >= lo) & (minute <= hi)]
        hits = win.index[win.to_numpy() > ref_mult * ref]
        if len(hits):
            flag.loc[hits] = True
    return flag


def _flag_auction_1min(df: pd.DataFrame, mult: float = 6.0, spill_mult: float = 3.0) -> pd.Series:
    """Core closing-cross detection on minute-level (or coarser) volume bars.

    Checks for whether closing occurs during "half-day" (around 13:00)
    or "full-day" (around 16:00).

    Parameters
    ----------
    df : pd.DataFrame
    mult : float, optional
        "Extreme-ness" threshold for Auction flag
        (default is 10.0x median)
    spill_mult : float, optional
        If volume is ___ times larger, then is also flagged as Auction
        (default is 3.0x median)
    
    Returns
    -------
    pd.Series
        A sequence of MINUTES which are flagged as Auction (True) or not (False)

    Notes
    -----
    The closing auction footprint spans two bars: the main cross on the last
    regular minute (~2500x a normal after-hours minute) plus an overflow/
    official print on the following one (~167x), after which volume drops back
    to normal. So:
      1. Pick the day's closing window — 15:59-16:01 normally, or 12:59-13:01
         when the day is a half-day. A half-day is decided by EVIDENCE, not by
         where the data happens to end: a genuine early close leaves only thin
         after-hours prints between 13:10 and 15:55 (well under the morning
         median), while a normal day being watched live before ~15:00 simply
         has no verdict yet — and gets NO early-close flag, so a 13:00 block
         trade can't be mistaken for the closing cross mid-session.
      2. anchor = the largest-volume bar inside the window, flagged only if it
         dwarfs the day's 09:30-13:00 median (> `mult`x) — a quiet day whose
         feed lacks the official cross has no anchor and nothing is flagged.
      3. spill  = walk FORWARD from the anchor, also flagging each following bar
         while it stays elevated (> `spill_mult`x the median), stopping at the
         first normal bar. This catches the 16:00 overflow but leaves later
         genuine after-hours and the pre-close ramp (15:55-15:58) untouched.
         See Personal Study Log §8.
    """
    flag = pd.Series(False, index=df.index) # init: no auction

    # PART 1: For each DAY (groups anomalies by day)...
    for d, g in df.groupby(lambda ix: ix.date()):
        if g.empty: continue

        minute = g.index.hour * 60 + g.index.minute

        # PART 2: Baseline ("regular-session") = Median volume between 09:30 - 13:00
        base = g.loc[(minute >= 570) & (minute < 780), "volume"]
        base_med = base.median() if not base.empty else 0
        if not base_med > 0:
            continue

        # PART 3: IF: Average vol. between 13:10 – 15:55 is <10% median, THEN: Is half-day
        aft = g.loc[(minute >= 790) & (minute <= 955), "volume"]
        if len(aft) >= 5 and aft.median() < base_med * 0.1:
            window_mask = (minute >= 779) & (minute <= 781) # Closing: Half-Day = 12:59-13:01
        else:
            window_mask = (minute >= 959) & (minute <= 961) # Closing: Full-Day = 15:59-16:01
        window = g.loc[window_mask, "volume"]
        if window.empty:
            continue
        
        # PART 4: Calculats the max volume in a 3-minute window + checks
        #         if exceeds the `mult` threshold for Auction detection
        anchor = window.idxmax()
        if not window.loc[anchor] > base_med * mult:
            continue
        flag.loc[anchor] = True

        # PART 4.1: Checks following minute-blocks for auction-"spillover"
        pos = g.index.get_loc(anchor)
        for ts in g.index[pos + 1:]:
            if g.loc[ts, "volume"] > base_med * spill_mult:
                flag.loc[ts] = True
            else:
                break
    
    return flag

# -----------------------------------------------------
# 3. Convert data to auction-filtered, buy/sell-aligned
# -----------------------------------------------------

def _apply_wick_decomp(df: pd.DataFrame) -> pd.DataFrame:
    """Converts Open-High-Low-Close-Volume to Buying-Volume/Selling-Volume/Delta
    
    Apply `decompose_candle` to every row of df; returns df with
    the three columns set.
    
    Parameters
    ----------
    pd.Dataframe 
        Rows contain Open-High-Low-Close-Volume and a timestamp-index
    
    Returns
    -------
    pd.Dataframe
        Rows contain buying vol., selling vol., and change in price b/t time-interval
    """

    results = df.apply(
        lambda row: decompose_candle(
            row["open"], row["high"], row["low"], row["close"], row["volume"]
        ),
        axis=1,
        result_type="expand",
    )
    df = df.copy()
    df["buying_volume"]  = results["buying_volume"]
    df["selling_volume"] = results["selling_volume"]
    df["delta"]          = results["delta"]
    return df


def _winsorize_delta(delta: pd.Series, q: float = 0.995) -> pd.Series:
    """Hides extreme delta (price-change) from the graph if it's `q`-abnormal
    
    Takes the q-percent-extremely-large-volume trades and ignores them.

    Parameters
    ----------
    delta : pd.Series
        A set of price-changes ("delta"-s) with timestamps
    
    Returns
    -------
    pd.Series
        A set of 

    Notes
    -----
    Cap each bar's |delta| at the per-session q-quantile, preserving sign.

    A single block/cross print (e.g. the ~1M-share cross NVDA printed at 15:53)
    lands entirely on one side and would otherwise dominate the CVD curve — the
    largest single trade accounts for ~35% of a 1-sec bar's gross flow and flips
    ~40% of 1-sec bars' direction. These mega-prints are crosses/dark-pool/combo
    legs, NOT aggressive lit flow, so capping their weight is a noise reduction,
    not a fit to price. Quantile is computed per trading day so a quiet day and a
    news day get their own scale.

    Requires a DatetimeIndex. Returns a Series aligned to `delta`. Operates
    positionally so it is safe on a raw-tick index with duplicate timestamps.
    """

    # PART 1: Splits the price-changes into days
    vals = delta.to_numpy(dtype=float).copy()
    days = np.asarray(delta.index.date)

    for day in np.unique(days):
        # PART 2.1: For each day, finds the q-large extreme data `cap`
        mask = days == day
        a = np.abs(vals[mask])
        cap = np.quantile(a, q)

        # PART 2.2. Caps extreme-volume trades to the minimum-extreme `cap`
        if cap > 0:
            vals[mask] = np.sign(vals[mask]) * np.minimum(a, cap)
    return pd.Series(vals, index=delta.index)


def add_cvd_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Return df with CVD-related columns added.

    Columns added:
        buying_volume  - buy volume per bar (real from ticks, or wick estimate)
        selling_volume - sell volume per bar
        delta          - buying_volume - selling_volume
        source         - 'ibkr_tick' | 'ibkr_hist' | 'finviz_wick'
        is_auction     - True for closing-auction bars (IBKR: volume removed;
                         FinViz: direction neutralized, volume kept)
        delta_raw      - delta before auction neutralization/removal
        auction_volume - kept auction volume still in `volume` (FinViz bars only;
                         removed IBKR auction is not counted here)
        cvd_all        - all-time cumulative delta (auction-neutralized)
        cvd_all_raw    - all-time cumulative delta (includes auction)

    Accepts both FinViz 1-min DataFrames and IBKR 1-sec DataFrames.
    If a 'source' column with value 'ibkr_tick' is present AND buying_volume
    is already populated, wick decomposition is skipped for those rows.
    """
    df = df.copy()

    # ── Date parsing / index ──────────────────────────────────────────────────
    # FinViz dates are strings "MM/DD/YYYY HH:MM AM/PM"; IBKR dates come from
    # MongoDB as native datetimes.  Detect by dtype and handle each separately.
    if "date" in df.columns:
        if pd.api.types.is_string_dtype(df["date"]):
            # FinViz format: "13:00 PM" suffix is redundant but present; strip it.
            df["date"] = pd.to_datetime(
                df["date"].astype(str).str.replace(r'\s*(AM|PM)$', '', regex=True),
                format="%m/%d/%Y %H:%M",
                errors="coerce",
            )
        else:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.set_index("date").sort_index()

    # ── Source-aware buy/sell decomposition ───────────────────────────────────
    #
    # IBKR tick data already has buying_volume / selling_volume / delta computed
    # per-tick in tick_collector.py (quote-based aggressor classification).
    # For those rows we skip decompose_candle entirely.
    # All other rows (FinViz, ibkr_hist, or NaN-filled ibkr_tick gaps) get wick
    # decomposition so the pipeline is seamless for mixed DataFrames.

    has_source = "source" in df.columns
    has_precomputed = (
        "buying_volume" in df.columns
        and "selling_volume" in df.columns
        and "delta" in df.columns
    )

    if has_precomputed:
        # Rows with a stored buy/sell/delta keep it — that covers ibkr_tick
        # (real aggressor) AND tier docs whose split was BVC-estimated at
        # write time (history package). Only rows missing any of the three
        # get the legacy wick decomposition.
        needs_wick = (
            df["buying_volume"].isna()
            | df["selling_volume"].isna()
            | df["delta"].isna()
        )

        if not has_source:
            df["source"] = pd.NA

        if needs_wick.any():
            decomp = _apply_wick_decomp(df[needs_wick])
            df.loc[needs_wick, "buying_volume"]  = decomp["buying_volume"]
            df.loc[needs_wick, "selling_volume"] = decomp["selling_volume"]
            df.loc[needs_wick, "delta"]          = decomp["delta"]
            # Tag rows that didn't already have a source
            no_src = needs_wick & df["source"].isna()
            if no_src.any():
                df.loc[no_src, "source"] = "finviz_wick"
    else:
        # No pre-computed values — apply wick decomposition to all rows.
        decomp = _apply_wick_decomp(df)
        df["buying_volume"]  = decomp["buying_volume"]
        df["selling_volume"] = decomp["selling_volume"]
        df["delta"]          = decomp["delta"]
        if "source" not in df.columns:
            df["source"] = "finviz_wick"

    # ── Closing-auction handling ──────────────────────────────────────────────
    # The 15:59/16:00 closing cross is a single-price doji carrying ~99% of its
    # minute as one batch auction (MOC); the wick model assigns its direction from
    # a 1-2 cent open/close difference, so its buy/sell SIGN is noise that would
    # otherwise dominate CVD (it alone drives the curve, ~26% of total |delta|).
    #
    # Handling depends on how confident the detection is:
    #   • IBKR (ibkr_tick / ibkr_hist): the cross is identified from the print's
    #     condition code + volume, so the flag is trustworthy → REMOVE the auction
    #     volume from the bar entirely (volume, buy, sell → 0). A batch MOC cross
    #     is not order flow, and dropping it leaves a clean volume profile with no
    #     grayed-out auction bar to read around.
    #   • FinViz (finviz_wick / unknown source): detection is only a volume
    #     heuristic (an estimate), so we KEEP the volume but NEUTRALIZE the split
    #     (buy = sell = volume/2, delta = 0) and gray the bar, in case the flag is
    #     a false positive.
    # In both cases an un-neutralized `delta_raw` is kept so a "CVD raw (incl.
    # auction)" comparison line stays available. See Personal Study Log §8.
    df["is_auction"]     = _flag_auction(df)

    # Ground-truth override: a bar whose stored AllLast condition code marks the
    # closing cross (see tick_collector's `special_conditions`) is an auction
    # regardless of the volume heuristic. The column is absent on estimated/older
    # bars, and _AUCTION_CONDITION_CODES is empty until real IBKR strings are
    # observed, so today this is a no-op that never mis-flags.
    if "special_conditions" in df.columns:
        code_auc = df["special_conditions"].map(_has_auction_condition)
        df["is_auction"] = df["is_auction"] | code_auc.fillna(False).astype(bool)

    df["delta_raw"]      = df["delta"]                       # before neutralize/remove
    auc = df["is_auction"]

    # Split auction bars by source confidence (see block comment above).
    ibkr_auc   = auc & df["source"].isin(["ibkr_tick", "ibkr_hist"])
    finviz_auc = auc & ~ibkr_auc

    # FinViz estimate → neutralize direction, keep the volume (grayed downstream).
    df.loc[finviz_auc, "buying_volume"]  = df.loc[finviz_auc, "volume"] / 2
    df.loc[finviz_auc, "selling_volume"] = df.loc[finviz_auc, "volume"] / 2
    df.loc[finviz_auc, "delta"]          = 0.0

    # IBKR confident → remove the auction volume from the bar entirely.
    df.loc[ibkr_auc, "buying_volume"]  = 0.0
    df.loc[ibkr_auc, "selling_volume"] = 0.0
    df.loc[ibkr_auc, "delta"]          = 0.0
    df.loc[ibkr_auc, "volume"]         = 0.0

    # Per-bar auction volume STILL counted in `volume` (finviz only; the removed
    # IBKR auction is no longer in `volume`, so it must not be double-counted or
    # gray its bar). Drives auction_frac → gray-out in the visualizer.
    df["auction_volume"] = df["volume"].where(finviz_auc, 0.0)

    # CVD (all-time): default = auction-neutralized; raw = includes the auction.
    df["cvd_all"]     = df["delta"].cumsum()
    df["cvd_all_raw"] = df["delta_raw"].cumsum()

    # ── Noise-reduced CVD variants (ADDITIVE — cvd_all above is unchanged) ─────
    # Infrastructure for the tick-CVD upgrade. Two independent noise reductions:
    #   delta_wins       — per-session winsorized delta (block/cross prints capped)
    #   cvd_session      — cumulative delta reset each trading day (drops prior-day
    #                      drift that the all-time cumsum drags forward; better for
    #                      intraday price tracking)
    #   cvd_wins         — all-time cumsum of the winsorized delta
    #   cvd_session_wins — session-anchored + winsorized (both fixes together)
    # NOTE: these improve the bar-level buy/sell signal and remove outlier
    # domination, but they do NOT by themselves fix the cumulative-LEVEL vs price
    # divergence — that is driven by systematic aggressor misclassification and
    # requires the quote-lag fix (see ibkr/tick_collector.py instrumentation and
    # scripts/analyze_quote_lag.py). Kept additive so nothing downstream breaks.
    df["delta_wins"]       = _winsorize_delta(df["delta"])
    # Session-anchored cumsums computed positionally (duplicate-timestamp safe on
    # a raw-tick index). groupby(...).cumsum() preserves row order, so .values
    # assignment aligns correctly even when the index has duplicate labels.
    _day = df.index.date
    df["cvd_session"]      = df.groupby(_day)["delta"].cumsum().values
    df["cvd_wins"]         = df["delta_wins"].cumsum().values
    df["cvd_session_wins"] = df.groupby(_day)["delta_wins"].cumsum().values

    # ── Independent BVC-estimated CVD (ADDITIVE — wick/tick columns untouched) ─
    # A parallel estimate of the same order flow using Bulk Volume Classification
    # on close-to-close moves, regardless of what produced buying_volume above.
    # Rendered as its own legend entry ("CVD (BVC est.)") so the tick/wick CVD
    # and the BVC CVD can be compared side by side on any chart.
    from history.bvc import bvc_split   # local import: keeps cvd↔history acyclic
    _bvc = bvc_split(df["close"], df["volume"])
    df["delta_bvc"] = _bvc["delta"].to_numpy()
    df.loc[auc, "delta_bvc"] = 0.0      # same closing-auction neutralization
    df["cvd_bvc"] = df["delta_bvc"].cumsum()

    # ── Pure wick-decomposition CVD, unified to the finest granularity ────────
    # delta_wick = (close-open)/(high-low)*volume — algebraically the same delta
    # decompose_candle produces. The tiers store it SUMMED from the finest tier
    # (IBKR 1-sec / FinViz 1-min; see history.rollup), so on every timeframe this
    # line is "finest-granularity wick, aggregated" rather than wick recomputed
    # on the coarse bar. When the column is absent (bare frames, un-rolled
    # history) we fall back to wick on df's own bars — still the finest data on
    # hand for that path. Rendered as its own legend entry ("CVD (wick est.)").
    if "delta_wick" in df.columns and df["delta_wick"].notna().any():
        dw = pd.to_numeric(df["delta_wick"], errors="coerce").to_numpy(dtype=float).copy()
        # Any rows the tier left unfilled get wick from their own OHLC.
        missing = np.isnan(dw)
        if missing.any():
            dw[missing] = _wick_delta_ohlc(df)[missing]
    else:
        dw = _wick_delta_ohlc(df)
    df["delta_wick"] = dw
    df.loc[auc, "delta_wick"] = 0.0     # same closing-auction neutralization
    df["cvd_wick"] = df["delta_wick"].cumsum()

    return df


def _wick_delta_ohlc(df: pd.DataFrame) -> np.ndarray:
    """Vectorized wick delta = (close-open)/(high-low)*volume for each row.
    Matches decompose_candle's delta (half-wick terms cancel); zero-range bars
    contribute 0. Used as the finest-data fallback when a rolled-up delta_wick
    column is not present on the frame."""
    spread = (df["high"] - df["low"]).to_numpy(dtype=float)
    direction = (df["close"] - df["open"]).to_numpy(dtype=float)
    vol = df["volume"].to_numpy(dtype=float)
    out = np.zeros(len(df), dtype=float)
    nz = spread > 0
    out[nz] = direction[nz] / spread[nz] * vol[nz]
    return out


# ─────────────────────────────────────────
# 3. Aggregate 1-min bars into N-min Buy/Sell Pressure
# ─────────────────────────────────────────

TIMEFRAME_RULE = {
    "1min":   "1min",
    "3min":   "3min",
    "5min":   "5min",
    "15min":  "15min",
    "1hr":    "1h",
    "3hr":    "3h",
    "1day":   "1D",
    "1week":  "1W-MON",
    "1month": "1ME",
}

# Extended rule set for IBKR 1-second base data; adds sub-minute timeframes.
# These two extra keys are omitted from TIMEFRAME_RULE (FinViz base) because
# resampling 1-min bars to 1-sec would upsample and produce empty rows.
TIMEFRAME_RULE_IBKR = {
    "raw_tick": "0S", # Special case for raw ticks
    "1sec":   "1s",
    "5sec":   "5s",
    "10sec":  "10s",
    "30sec":  "30s",
    **TIMEFRAME_RULE,
}

# Timeframes at or above daily granularity (no intraday hour breaks on x-axis)
DAILY_OR_ABOVE = {"1day", "1week", "1month"}

# Timeframes where weekend breaks must NOT be applied (labels can land on weekends)
WEEK_OR_ABOVE = {"1week", "1month"}

def aggregate_pressure(df_base: pd.DataFrame, timeframe: str = "1hr") -> pd.DataFrame:
    """
    Resample and aggregate a base-granularity DataFrame (1-min for FinViz,
    1-sec for IBKR) to the requested timeframe.

    timeframe: one of the keys in TIMEFRAME_RULE or TIMEFRAME_RULE_IBKR
               (e.g. "1sec", "1min", "1hr", "1day").
    """
    _all_rules = {**TIMEFRAME_RULE, **TIMEFRAME_RULE_IBKR}
    rule = _all_rules.get(timeframe, timeframe)  # Use custom string directly if not in rules
    df_1min = df_base  # rename kept for clarity; works for any base granularity

    agg_spec = dict(
        open=("open",                "first"),
        high=("high",                "max"),
        low=("low",                  "min"),
        close=("close",              "last"),
        volume=("volume",            "sum"),
        buy_pressure=("buying_volume",  "sum"),
        sell_pressure=("selling_volume","sum"),
        delta_sum=("delta",          "sum"),
        cvd_all_end=("cvd_all",      "last"),   # CVD all-time (auction-neutralized)
        cvd_all_raw_end=("cvd_all_raw", "last"),# CVD all-time (includes auction)
        auction_vol=("auction_volume", "sum"),  # auction volume falling in this bar
    )
    # Noise-reduced CVD variants (carried through only when add_cvd_columns
    # produced them, so older callers / bare frames still resample cleanly).
    for src, dst, how in [
        ("delta_wins",       "delta_wins_sum",       "sum"),
        ("cvd_session",      "cvd_session_end",      "last"),
        ("cvd_wins",         "cvd_wins_end",         "last"),
        ("cvd_session_wins", "cvd_session_wins_end", "last"),
        ("delta_bvc",        "delta_bvc_sum",        "sum"),
        ("cvd_bvc",          "cvd_bvc_end",          "last"),
        ("delta_wick",       "delta_wick_sum",       "sum"),
        ("cvd_wick",         "cvd_wick_end",         "last"),
    ]:
        if src in df_1min.columns:
            agg_spec[dst] = (src, how)
    if "source" in df_1min.columns:
        # Carry the (dominant) data source so the visualizer can shade
        # wick-estimated regions on every timeframe, not just the base one.
        agg_spec["source"] = ("source", "first")
    df_agg = df_1min.resample(rule).agg(**agg_spec).dropna(subset=["open"])

    if "quality" in df_1min.columns:
        # Worst-of quality per bucket: a coarse bar mixing real tick flow and
        # estimates must still read as estimated ('mixed') in the chart.
        # Vectorized via rank min/max — a Python reducer per bucket cost ~1s
        # on a 19k-row 1-min frame (one call per output bar).
        _RANK = {"tick": 4, "mixed": 3, "bvc": 2, "wick": 1, "neutral": 0}
        _FROM_RANK = {v: k for k, v in _RANK.items()}
        rank = df_1min["quality"].map(_RANK)
        rmin = rank.resample(rule).min()
        rmax = rank.resample(rule).max()
        q = rmin.map(_FROM_RANK)                      # single quality → itself
        q[(rmin != rmax) & (rmax == _RANK["tick"])] = "mixed"  # tick + estimates
        # other mixtures already read as their worst (lowest-rank) member
        df_agg["quality"] = q.reindex(df_agg.index)

    df_agg["net_pressure"] = df_agg["buy_pressure"] - df_agg["sell_pressure"]

    # Fraction of this bar's volume that came from the closing auction. Used to
    # gray out auction-dominated bars in the chart (their direction isn't real):
    # >0.5 on the 15:59-containing intraday bars, ~0.2 on a whole-day bar.
    df_agg["auction_frac"] = (df_agg["auction_vol"] / df_agg["volume"]).fillna(0.0)

    # ── Momentum: Pressure ROC (Rate of Change)
    #   ROC_t = (Pressure_t − Pressure_{t-n}) / Pressure_{t-n} × 100
    # Percent change vs. the previous bar (n=1): measures how fast buy/sell
    # pressure is accelerating or decelerating.
    #
    # Note: computed within each session (by date) so it never compares against
    # the previous day's last bar. Also, the pre-market (low volume) -> regular
    # open (high volume) transition makes the denominator tiny and blows the ROC
    # up, so we clip to ±ROC_CLIP% to suppress spikes and keep only the trend.
    ROC_CLIP = 200.0
    d = df_agg.index.date
    df_agg["buy_pressure_roc"]  = (df_agg.groupby(d)["buy_pressure"].pct_change()  * 100).clip(-ROC_CLIP, ROC_CLIP)
    df_agg["sell_pressure_roc"] = (df_agg.groupby(d)["sell_pressure"].pct_change() * 100).clip(-ROC_CLIP, ROC_CLIP)

    # ── ROC computed using regular-hours bars only (09:30~16:00)
    # Comparing regular-hours bars to each other avoids the after/pre-market jump.
    minutes = df_agg.index.hour * 60 + df_agg.index.minute
    reg_mask = (minutes >= 570) & (minutes < 960)   # 9:30 ~ 16:00
    for src, dst in [("buy_pressure", "buy_roc_reg"), ("sell_pressure", "sell_roc_reg")]:
        reg = df_agg.loc[reg_mask, src]
        roc = (reg.groupby(reg.index.date).pct_change() * 100).clip(-ROC_CLIP, ROC_CLIP)
        df_agg[dst] = roc.reindex(df_agg.index)      # value on regular-hours bars only, NaN elsewhere

    return df_agg


def _reclassify_raw_docs(docs: list[dict], max_age_ms: float) -> list[dict]:
    """Overwrite each raw-tick doc's `delta` using stale-quote demotion.

    Ticks must be re-sorted by time (the tick rule is sequential). Docs missing
    bid/ask/quote_age_ms (pre-instrumentation ticks) keep their stored delta.
    """
    from cvd.aggressor import classify_demote_stale

    docs = sorted(docs, key=lambda d: d["date"])
    have = [("bid" in d and "ask" in d and "quote_age_ms" in d) for d in docs]
    if not any(have):
        return docs  # nothing instrumented; leave stored delta untouched

    price = np.array([d["price"] for d in docs], dtype=float)
    size  = np.array([d["size"]  for d in docs], dtype=float)
    bid   = np.array([d.get("bid", np.nan) for d in docs], dtype=float)
    ask   = np.array([d.get("ask", np.nan) for d in docs], dtype=float)
    age   = np.array([d.get("quote_age_ms", np.nan) for d in docs], dtype=float)

    new_delta = classify_demote_stale(price, size, bid, ask, age, max_age_ms=max_age_ms)
    for d, nd, ok in zip(docs, new_delta, have):
        if ok:                       # only override instrumented ticks
            d["delta"] = float(nd)
    return docs


# ─────────────────────────────────────────
# 4. Load data from MongoDB
# ─────────────────────────────────────────

def load_from_mongo(
    ticker: str,
    timeframe: str = "i1",
    days: int | None = None,
    reclassify_stale_ms: float | None = None,
) -> pd.DataFrame:
    """
    Load a given ticker/timeframe from MongoDB finviz_db.candles.

    Returns all OHLCV columns plus pre-computed IBKR columns when present:
    buying_volume, selling_volume, delta, source.  add_cvd_columns() uses
    these to skip wick decomposition for ibkr_tick rows.

    days: only load bars from the last `days` calendar days. Keeps chart
          regeneration fast once weeks of 1-sec data accumulate. Only valid
          for tick-based timeframes whose `date` field is a real datetime
          (FinViz 'i1' docs store dates as strings, which a datetime range
          query would silently exclude).

    reclassify_stale_ms: (raw_tick only) if set, re-derive each tick's buy/sell
          delta with quotes older than this many ms demoted to the tick rule
          (quote-lag fix). Requires instrumented ticks carrying bid/ask/
          quote_age_ms; ticks without them keep their stored delta. None (default)
          uses the delta as computed live by tick_collector.
    """
    client = MongoClient("mongodb://localhost:27017/")
    if timeframe == "raw_tick":
        collection = client["finviz_db"]["raw_ticks"]
        query = {"ticker": ticker}
        if days is not None:
            now_et = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
            query["date"] = {"$gte": now_et - timedelta(days=days)}
        proj = {"_id": 0, "date": 1, "price": 1, "size": 1, "delta": 1, "source": 1}
        if reclassify_stale_ms is not None:
            proj.update({"bid": 1, "ask": 1, "quote_age_ms": 1})
        docs = list(collection.find(query, proj))

        if reclassify_stale_ms is not None and docs:
            docs = _reclassify_raw_docs(docs, reclassify_stale_ms)

        # Convert raw ticks to OHLCV format so the rest of the pipeline works seamlessly
        for doc in docs:
            p = doc.pop("price")
            s = doc.pop("size")
            d = doc["delta"]
            doc["open"] = doc["high"] = doc["low"] = doc["close"] = p
            doc["volume"] = s
            doc["buying_volume"] = s if d > 0 else 0.0
            doc["selling_volume"] = s if d < 0 else 0.0
    else:
        collection = client["finviz_db"]["candles"]
        query = {"ticker": ticker, "timeframe": timeframe}
        if days is not None and timeframe != "i1":
            now_et = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
            cutoff = now_et - timedelta(days=days)
            query["date"] = {"$gte": cutoff}
        
        docs = list(collection.find(
            query,
            {
                "_id": 0,
                "date": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
                # Pre-computed by tick_collector (ibkr_tick) — absent for FinViz docs.
                "buying_volume": 1, "selling_volume": 1, "delta": 1, "source": 1,
                "quality": 1,
            }
        ))

    if not docs:
        print(f"[MongoDB] No data found for {ticker} ({timeframe})")
        return pd.DataFrame()

    df = pd.DataFrame(docs)
    print(f"[MongoDB] Loaded {len(df)} candles for {ticker} ({timeframe})")
    return df


# ─────────────────────────────────────────
# 5. Run the whole pipeline at once
# ─────────────────────────────────────────

def run_pipeline(
    ticker: str,
    base_timeframe: str = "i1",
    days: int | None = None,
    only: list[str] | None = None,
) -> tuple[pd.DataFrame, dict]:
    """
    Load bars from MongoDB → compute CVD → aggregate into every timeframe.

    Args:
        ticker         : stock symbol (e.g. 'NVDA')
        base_timeframe : MongoDB timeframe field to load.
                         'i1'   → FinViz 1-min bars (default, backward-compat)
                         '1sec' → IBKR 1-second bars (tick_collector output)
        days           : only load the last N days (tick timeframes only;
                         see load_from_mongo). None = everything.
        only           : restrict frames to these timeframe labels. The Dash
                         app displays a single timeframe, so aggregating all
                         12 on every callback is wasted work; None (default)
                         keeps the build-everything behavior for the static
                         HTML chart with timeframe buttons.

    Returns:
        df_base : base-granularity bars with buy/sell/delta/cvd columns
        frames  : dict of aggregated DataFrames keyed by timeframe label
                  FinViz: {"1min": df, ..., "1month": df}
                  IBKR  : {"1sec": df, "5sec": df, "1min": df, ..., "1month": df}
    """
    print(f"\n{'='*50}")
    print(f"  Pipeline: {ticker}  (base_timeframe={base_timeframe})")
    print(f"{'='*50}")

    df_raw = load_from_mongo(ticker, timeframe=base_timeframe, days=days)
    if df_raw.empty:
        return pd.DataFrame(), {}

    df_base = add_cvd_columns(df_raw)

    # Choose the right timeframe map: IBKR adds 1sec / 5sec buttons to the chart.
    if base_timeframe in ["1sec", "raw_tick"]:
        tf_map = dict(TIMEFRAME_RULE_IBKR)
    else:
        tf_map = dict(TIMEFRAME_RULE)
        
    if only is not None:
        for tf in only:
            if tf not in tf_map:
                tf_map[tf] = tf  # Allow custom timeframe strings
    frames = {}
    for tf, rule in tf_map.items():
        if only is not None and tf not in only:
            continue
        if tf == "raw_tick":
            raw_df = df_base.copy()
            raw_df = raw_df.rename(columns={
                "buying_volume": "buy_pressure",
                "selling_volume": "sell_pressure",
                "cvd_all": "cvd_all_end",
                "cvd_all_raw": "cvd_all_raw_end",
                "cvd_bvc": "cvd_bvc_end",
                "cvd_wick": "cvd_wick_end",
                "auction_volume": "auction_vol"
            })
            raw_df["net_pressure"] = raw_df["buy_pressure"] - raw_df["sell_pressure"]
            raw_df["auction_frac"] = (raw_df["auction_vol"] / raw_df["volume"]).fillna(0.0) if "auction_vol" in raw_df.columns else 0.0
            frames["raw_tick"] = raw_df
            continue
        frames[tf] = aggregate_pressure(df_base, tf)

    sources = df_base["source"].value_counts().to_dict() if "source" in df_base.columns else {}
    print(f"[Pipeline] Base bars  : {len(df_base)}  sources={sources}")
    for tf, df in frames.items():
        print(f"[Pipeline] {tf:>6} bars : {len(df)}")
    print(f"[Pipeline] CVD range  : {df_base['cvd_all'].min():.0f} ~ {df_base['cvd_all'].max():.0f}")
    n_auc = int(df_base["is_auction"].sum())
    auc_vol = df_base["auction_volume"].sum()
    print(f"[Pipeline] Auctions   : {n_auc} bars neutralized, total auction vol {auc_vol:,.0f}")
    print(f"[Pipeline] Done.\n")

    return df_base, frames


# ── Quick test when run directly
if __name__ == "__main__":
    import sys
    base_tf = sys.argv[1] if len(sys.argv) > 1 else "i1"
    df_base, frames = run_pipeline("NVDA", base_timeframe=base_tf)
    if not df_base.empty:
        cols = ["open", "close", "volume", "buying_volume", "selling_volume", "delta", "cvd_all"]
        if "source" in df_base.columns:
            cols.insert(0, "source")
        print(f"\n[Base ({base_tf}) sample (last 3 rows)]")
        print(df_base[cols].tail(3).to_string())
        print("\n[1-hour sample (last 3 rows)]")
        print(frames["1hr"][["open","close","buy_pressure","sell_pressure","net_pressure","cvd_all_end"]].tail(3).to_string())
