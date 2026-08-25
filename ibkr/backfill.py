"""
ibkr/backfill.py — historical bar backfill into the resolution-ladder tiers.

Supports multiple bar sizes (see history/schema.py tiers):
  --barsize 1sec   (default) 1-sec bars, 30-min chunks.  IBKR caps bars
                   <= 30 sec at a 6-MONTH lookback.
  --barsize 1min   1-min bars, 1-day chunks. No lookback cap (years OK).
  --barsize 30min  30-min bars, 1-month chunks. Fills the 1hr/3hr charts.
  --barsize 1day   daily bars, 1-year chunks. Cross-check for FinViz daily.

All fetched bars are OHLCV only (no tick-level quotes), so the buy/sell
split is estimated at write time with BVC (history/bvc.py) and stored with
source='ibkr_hist', quality='bvc'. The quality guard in history.store
guarantees these estimates can never overwrite live tick-classified bars.

Progress is recorded in backfill_meta per (ticker, timeframe); re-running the
same command resumes past the already-covered range instead of re-fetching.

Pacing rules (IBKR): max 60 historical requests / 10 min, identical requests
15 s apart. We wait PACING_DELAY between requests and back off on error 162.

Usage:
    python -m ibkr.backfill --ticker NVDA --days 1                    # 1-sec
    python -m ibkr.backfill --ticker NVDA --barsize 30min --days 730
    python -m ibkr.backfill --ticker NVDA --barsize 1min --days 180
    python -m ibkr.backfill --ticker NVDA --start 20260601 --end 20260628
"""

import asyncio
import argparse
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from ib_async import IB, Stock

from history.bvc import bvc_split
from history.schema import mongo_client
from history.store import get_backfill_coverage, set_backfill_coverage, upsert_bars

ET = ZoneInfo("America/New_York")

# The machine running IB Gateway / TWS. Loopback is right for every local run
# and stays the default; a container deploy is the case that needs it
# overridden, because there Gateway is a separate service and 127.0.0.1 is this
# process's own container. Set IB_HOST to the Gateway service name.
IB_HOST = os.environ.get("IB_HOST", "127.0.0.1")

PACING_DELAY = 11.0     # wait between requests: 11 s → ≈54 req/10 min (< 60 limit)
PACING_BACKOFF = 60.0   # wait after a pacing-violation error

# barsize key → IBKR barSizeSetting, chunk span per request, IBKR durationStr
BAR_CONFIG = {
    "1sec":  {"setting": "1 secs",  "chunk": timedelta(seconds=1800), "duration": None},   # "<sec> S"
    "1min":  {"setting": "1 min",   "chunk": timedelta(days=1),       "duration": "1 D"},
    "30min": {"setting": "30 mins", "chunk": timedelta(days=30),      "duration": "1 M"},
    "1day":  {"setting": "1 day",   "chunk": timedelta(days=365),     "duration": "1 Y"},
}


def _parse_bar_time(raw_time) -> datetime:
    """Convert IBKR bar time (datetime, date, or 'YYYYMMDD  HH:MM:SS') to ET naive."""
    if isinstance(raw_time, datetime):
        if raw_time.tzinfo:
            return raw_time.astimezone(ET).replace(tzinfo=None)
        return raw_time
    s = str(raw_time).strip()
    if len(s) == 8 and s.isdigit():          # daily bars: 'YYYYMMDD'
        return datetime.strptime(s, "%Y%m%d")
    if "-" in s and ":" not in s:            # daily bars as date object str
        return datetime.strptime(s, "%Y-%m-%d")
    return datetime.strptime(s, "%Y%m%d  %H:%M:%S")


def _bars_to_docs(ticker: str, bars, tf: str) -> list[dict]:
    """IB BarData list → tier docs with write-time BVC buy/sell estimation."""
    rows = []
    for bar in bars:
        try:
            rows.append({
                "date": _parse_bar_time(bar.date),
                "open": float(bar.open), "high": float(bar.high),
                "low": float(bar.low), "close": float(bar.close),
                "volume": float(bar.volume),
            })
        except Exception as e:
            logging.warning(f"Skipping malformed bar {bar}: {e}")
    if not rows:
        return []

    df = pd.DataFrame(rows).sort_values("date").set_index("date")
    df = df[~df.index.duplicated(keep="last")]
    est = bvc_split(df["close"], df["volume"])

    return [{
        "ticker": ticker.upper(),
        "timeframe": tf,
        "date": ts.to_pydatetime(),
        "open": float(r["open"]), "high": float(r["high"]),
        "low": float(r["low"]), "close": float(r["close"]),
        "volume": float(r["volume"]),
        "buying_volume": float(est.loc[ts, "buying_volume"]),
        "selling_volume": float(est.loc[ts, "selling_volume"]),
        "delta": float(est.loc[ts, "delta"]),
        "source": "ibkr_hist",
        "quality": "bvc",
    } for ts, r in df.iterrows()]


async def backfill_ticker(
    ticker: str,
    start: datetime,
    end: datetime,
    barsize: str = "1sec",
    port: int = 7497,
    client_id: int = 11,
    resume: bool = True,
):
    """
    Backfill `barsize` bars for `ticker` from `start` to `end` (ET naive).
    Iterates backwards in chunks; pacing-aware; resumes past covered ranges.
    """
    cfg = BAR_CONFIG[barsize]
    logging.info(
        f"[Backfill] {ticker} ({barsize}): {start.strftime('%Y-%m-%d %H:%M')} → "
        f"{end.strftime('%Y-%m-%d %H:%M')} ET"
    )

    mongo = mongo_client()
    cov = get_backfill_coverage(ticker.upper(), barsize, client=mongo) if resume else None
    if cov and cov["start"] <= start and cov["end"] >= end:
        logging.info(f"[Backfill] Range already covered ({cov['start']} → {cov['end']}). Nothing to do.")
        return

    ib = IB()
    try:
        await ib.connectAsync(IB_HOST, port, clientId=client_id)
    except Exception as e:
        logging.error(f"[Backfill] Connection failed: {e}")
        return

    contract = Stock(ticker.upper(), "SMART", "USD")
    await ib.qualifyContractsAsync(contract)

    total_saved = 0
    current_end = end
    request_count = 0

    try:
        while current_end > start:
            # Skip through the already-covered range (resume support).
            if cov and cov["start"] < current_end <= cov["end"]:
                logging.info(f"  Skipping covered range down to {cov['start']}")
                current_end = cov["start"]
                continue

            chunk_start = max(current_end - cfg["chunk"], start)
            if cfg["duration"] is None:
                actual_secs = int((current_end - chunk_start).total_seconds())
                if actual_secs <= 0:
                    break
                duration = f"{actual_secs} S"
            else:
                duration = cfg["duration"]

            end_dt_aware = current_end.replace(tzinfo=ET)
            logging.info(
                f"  Request {request_count+1}: "
                f"{chunk_start.strftime('%Y-%m-%d %H:%M')} – {current_end.strftime('%Y-%m-%d %H:%M')} "
                f"({duration})..."
            )

            try:
                bars = await ib.reqHistoricalDataAsync(
                    contract,
                    endDateTime=end_dt_aware,
                    durationStr=duration,
                    barSizeSetting=cfg["setting"],
                    whatToShow="TRADES",
                    useRTH=False,
                    formatDate=1,
                    keepUpToDate=False,
                )
                request_count += 1
            except Exception as e:
                err = str(e)
                if "162" in err or "pacing" in err.lower():
                    logging.warning(f"  Pacing violation — waiting {PACING_BACKOFF}s...")
                    await asyncio.sleep(PACING_BACKOFF)
                    continue
                logging.error(f"  Request error: {e}")
                await asyncio.sleep(10)
                continue

            if bars:
                docs = _bars_to_docs(ticker, bars, barsize)
                if docs:
                    saved = upsert_bars(docs, client=mongo)
                    total_saved += saved
                    logging.info(f"    Saved {saved} bars (fetched {len(docs)}). Total: {total_saved}")
            else:
                logging.info("    No bars returned (outside market hours or no trades).")

            # Record progress so an interrupted run resumes here. Only reload
            # the coverage when resuming: with resume=False the whole point is
            # to refetch the requested range, and the coverage union (which
            # can't represent gaps) would otherwise "cover" it after the very
            # first chunk and end the run.
            set_backfill_coverage(ticker.upper(), barsize, chunk_start, current_end, client=mongo)
            if resume:
                cov = get_backfill_coverage(ticker.upper(), barsize, client=mongo)
            current_end = chunk_start

            if current_end > start:
                await asyncio.sleep(PACING_DELAY)

    finally:
        ib.disconnect()
        logging.info(f"[Backfill] Done. Total saved: {total_saved} bars for {ticker} ({barsize}).")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Backfill IBKR historical bars → candles tiers (source='ibkr_hist', BVC split)"
    )
    parser.add_argument("--ticker", nargs="+", default=["NVDA"])
    parser.add_argument(
        "--barsize", default="1sec", choices=list(BAR_CONFIG.keys()),
        help="Bar size / destination tier (default: 1sec)",
    )
    parser.add_argument(
        "--days", type=int, default=1,
        help="Number of calendar days to backfill ending now (default: 1)",
    )
    parser.add_argument(
        "--start", default=None,
        help="Start date YYYYMMDD ET (overrides --days, e.g. 20260601)",
    )
    parser.add_argument(
        "--end", default=None,
        help="End date YYYYMMDD ET (default: now, e.g. 20260628)",
    )
    parser.add_argument(
        "--port", type=int, default=7497,
        help="IB Gateway port (default: 7497 paper TWS)",
    )
    parser.add_argument(
        "--client-id", type=int, default=11, dest="client_id",
        help="clientId for this connection (must differ from tick_collector's)",
    )
    parser.add_argument(
        "--no-resume", action="store_true",
        help="Ignore backfill_meta coverage and re-fetch everything",
    )
    args = parser.parse_args()

    now = datetime.now(tz=ET).replace(tzinfo=None)
    end_dt = (
        datetime.strptime(args.end, "%Y%m%d").replace(hour=23, minute=59, second=59)
        if args.end else now
    )
    start_dt = (
        datetime.strptime(args.start, "%Y%m%d")
        if args.start else (now - timedelta(days=args.days))
    )

    for t in args.ticker:
        asyncio.run(backfill_ticker(
            t, start_dt, end_dt, barsize=args.barsize,
            port=args.port, client_id=args.client_id, resume=not args.no_resume,
        ))


if __name__ == "__main__":
    main()
