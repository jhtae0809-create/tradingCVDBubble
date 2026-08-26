"""
ibkr/tick_collector.py — Step 2: Real-time tick-by-tick collector.

Subscribes to two IBKR data streams simultaneously:
  - AllLast  (trade ticks)  : price, size, time for each execution
  - BidAsk   (quote ticks)  : best bid/ask at each update

Each trade tick is classified as buy/sell aggressor using the concurrent
bid/ask quote (quote-based method, ~50 lines). Classified ticks are then
accumulated within 1-second buckets and flushed to MongoDB as 1-sec OHLCV
bars with pre-computed buying_volume / selling_volume / delta columns.

These bars are tagged source='ibkr_tick', which causes calculator.py to skip
the wick-decomposition step and use the real buy/sell values directly.

Timestamps are converted from UTC to US/Eastern so the auction-detection
logic in calculator.py (which checks for 15:59 / 16:00 ET) still works.

Reconnection: run() detects a dropped API connection (isConnected() → False,
e.g. the gateway's daily/weekly restart) and re-connects + re-subscribes both
tick streams itself — ib_async does NOT auto-resubscribe, so without this the
process would stay alive but silently collect nothing. The weekly Sunday 1am ET
forced *logout* still requires IBC to re-authenticate the gateway (external
setup, see DONE.md); this loop handles every drop where the gateway comes back.

Usage:
    python -m ibkr.tick_collector --ticker NVDA
    python -m ibkr.tick_collector --ticker NVDA --port 7496   # live TWS
    python -m ibkr.tick_collector --ticker NVDA AAPL TSLA     # multi-ticker

Ports: TWS paper=7497, TWS live=7496, IB Gateway paper=4002, live=4001
"""

import asyncio
import argparse
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pymongo import MongoClient
from ib_async import IB, Stock

# Shared aggressor classification (same logic as the Alpaca pipeline).
# Note: during the closing auction (15:59 ET) the entire MOC order book
# clears at one price, so all ticks land on either pure-buy or pure-sell —
# same noise issue as wick decomp; _flag_auction() in calculator.py
# neutralizes those bars.
from cvd.aggressor import classify_aggressor, next_tick_dir

ET = ZoneInfo("America/New_York")

# Overridable so the app can run against a MongoDB that is not on this
# machine. Railway (and any container deploy) runs the database as a
# separate service, where "localhost" is the app container itself and
# resolves to nothing. Default unchanged, so local runs are unaffected.
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME = "finviz_db"

# The machine running IB Gateway / TWS. Loopback is right for every local run
# and stays the default; a container deploy is the case that needs it
# overridden, because there Gateway is a separate service and 127.0.0.1 is this
# process's own container. Set IB_HOST to the Gateway service name.
IB_HOST = os.environ.get("IB_HOST", "127.0.0.1")

# Connect read-only. Nothing in this repo ever places an order, and saying so
# at connect time matters operationally: when Gateway runs with
# ReadOnlyApi=yes — which a deployment on someone else's account should — a
# client that asks for write access makes Gateway pop an "API client needs
# write access action confirmation" dialog. On a headless container nobody
# clicks it, so the connection just times out with no hint of the cause.
READONLY = True

# Flush the NBBO buffer once it reaches this many quotes even if no trade has
# rolled the 1-sec bar over. Quotes update far more often than trades, so during
# quote-only periods (halts, thin symbols, pre-market) the bar flush may not fire
# for a long time — without this cap the buffer would grow unbounded in memory
# and none of those quotes would be persisted to raw_quotes.
QUOTE_FLUSH_LIMIT = 2000

# NBBO price-band outlier filter. IBKR's AllLast stream includes special-condition
# prints (out-of-sequence, derivatively-priced, odd venues) that the consolidated
# tape excludes from OHLC — in thin after-hours trading these land far from the
# real price (e.g. a single 222 or 193 print while NVDA is ~207) and blow out a
# bar's high/low wick. A trade is treated as aberrant when it prints more than
# NBBO_BAND beyond the prevailing NBBO (bid*(1-band) .. ask*(1+band)); such prints
# are EXCLUDED from the bar's O/H/L/C but still stored verbatim in raw_ticks and
# still counted in volume/delta (a bad print is tiny size, and raw stays raw).
# Filtering is skipped when no valid NBBO is known (can't judge → keep the print).
NBBO_BAND = float(os.environ.get("NBBO_BAND", "0.015"))


# ─────────────────────────────────────────
# TickCollector class
# ─────────────────────────────────────────

class TickCollector:
    """
    Connects to IB Gateway and streams tick-by-tick data for one ticker.

    Internal state:
        bid / ask           — latest NBBO from the BidAsk stream
        current_second      — ET datetime (second precision) of the open bucket
        tick_buffer         — list of (price, size, signed_delta) within the bucket
        prev_trade_price    — last trade price (for tick-rule fallback)
    """

    def __init__(self, ticker: str, port: int = 7497, client_id: int = 10):
        self.ticker = ticker.upper()
        self.port = port
        self.client_id = client_id
        self.ib = IB()

        self.bid: float | None = None
        self.ask: float | None = None
        # Best bid/ask SIZES (queue depth at the touch), needed for Order-Flow
        # Imbalance (OFI, Cont-Kukanov-Stoikov). Carried alongside bid/ask so both
        # the per-trade snapshot and the raw_quotes stream record queue state.
        self.bid_size: float | None = None
        self.ask_size: float | None = None
        # Timestamp (ET naive) of the most recent bid/ask update. Used to measure
        # quote-lag: how stale the NBBO was when a trade tick was classified.
        self.quote_time: datetime | None = None
        self.prev_trade_price: float | None = None
        self.prev_tick_dir: float = 0.0

        self.current_second: datetime | None = None
        self.tick_buffer: list[tuple[float, float, float]] = []
        self.raw_buffer = []
        self.quote_buffer = []
        # Distinct AllLast specialConditions strings seen in the current second.
        # Captured so the closing-auction print can eventually be flagged by its
        # exchange condition code (the ground-truth signal) instead of the volume
        # heuristic — see cvd.calculator._flag_auction. IBKR's exact code strings
        # for the closing cross are undocumented, so we STORE them now and add the
        # matcher once real values are observed at 16:00.
        self.cond_seen: set[str] = set()

        mongo = MongoClient(MONGO_URI)
        self.col = mongo[DB_NAME]["candles"]
        self.raw_col = mongo[DB_NAME]["raw_ticks"]
        # Full NBBO stream, persisted so trades can be re-classified OFFLINE by
        # time-aligning each trade to the quote prevailing at its timestamp
        # (merge_asof, the industry-standard Lee-Ready fix for quote-lag). The
        # real-time per-trade bid/ask snapshot in raw_ticks can be stale; this
        # stream lets us recover the correct prevailing quote after the fact.
        self.quote_col = mongo[DB_NAME]["raw_quotes"]
        self.quote_col.create_index([("ticker", 1), ("date", 1)], background=True)
        self.raw_col.create_index([("ticker", 1), ("date", 1)], background=True)
        self.col.create_index(
            [("ticker", 1), ("timeframe", 1), ("date", 1)],
            unique=True,
            name="ticker_tf_date",
            background=True,
        )

    # ── Event handlers ──────────────────────────────────────────────────────

    @staticmethod
    def _to_et(ts) -> datetime:
        """Normalize an IBKR tick time to an ET-naive datetime (microsecond precision)."""
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts.astimezone(ET).replace(tzinfo=None)
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(ET).replace(tzinfo=None)

    def _classify_method(self, price: float) -> str:
        """Which rule classify_aggressor() used, for quote-lag diagnostics.

        'quote'    — decided by NBBO (price at/through bid or ask)
        'tick'     — quote unusable/inside spread; decided by up/down tick rule
        'zerotick' — price unchanged; inherited previous tick direction
        'none'     — nothing applied (no quote, no prior trade) -> delta 0
        """
        if self.bid is not None and self.ask is not None and self.bid < self.ask:
            if price >= self.ask or price <= self.bid:
                return "quote"
        if self.prev_trade_price is not None and price != self.prev_trade_price:
            return "tick"
        return "zerotick" if self.prev_tick_dir else "none"

    def _price_clean(self, price: float) -> bool:
        """True if `price` is within NBBO_BAND of the prevailing NBBO (or if the
        NBBO is unknown, so we never drop prints we can't judge). Used to keep
        special-condition outlier prints out of the OHLC wick."""
        b, a = self.bid, self.ask
        if not b or not a or b <= 0 or a <= 0 or b > a:
            return True  # no usable NBBO -> can't judge -> keep
        return b * (1.0 - NBBO_BAND) <= price <= a * (1.0 + NBBO_BAND)

    def _on_trade_tick(self, ticker_obj):
        """Called by ib_async when new AllLast (trade) ticks arrive."""
        for tick in ticker_obj.tickByTicks:
            ts = self._to_et(tick.time)
            sec = ts.replace(microsecond=0)

            # Boundary: flush completed second, start new bucket
            if sec != self.current_second:
                if self.current_second is not None:
                    self._flush(self.current_second)
                self.current_second = sec
                self.tick_buffer = []
                self.cond_seen = set()

            price = float(tick.price)
            size = float(tick.size)
            # AllLast condition string (undocumented for the closing cross;
            # captured raw for offline verification). May be "" for regular sales.
            cond = (getattr(tick, "specialConditions", "") or "").strip()
            if cond:
                self.cond_seen.add(cond)
            # Snapshot the classification path + NBBO staleness BEFORE mutating
            # prev-tick state, so we can audit quote-lag offline.
            method = self._classify_method(price)
            quote_age_ms = (
                (ts - self.quote_time).total_seconds() * 1000.0
                if self.quote_time is not None else None
            )
            delta = classify_aggressor(
                price, size, self.bid, self.ask,
                self.prev_trade_price, self.prev_tick_dir,
            )
            self.prev_tick_dir = next_tick_dir(price, self.prev_trade_price, self.prev_tick_dir)
            self.prev_trade_price = price
            # 4th element = clean flag: False for NBBO-band outliers, so _flush can
            # exclude them from O/H/L/C while keeping them in volume/delta and raw.
            self.tick_buffer.append((price, size, delta, self._price_clean(price)))

            # Save raw tick (+ quote-lag diagnostics: NBBO seen at classify time,
            # how stale it was, and which rule fired). These let us measure
            # whether the mid-size sell skew is quote-lag misclassification.
            self.raw_buffer.append({
                "ticker": self.ticker,
                "date": ts, # Exact datetime with microsecond precision
                "price": price,
                "size": size,
                "delta": delta,
                "source": "ibkr_tick",
                "bid": self.bid,
                "ask": self.ask,
                "bid_size": self.bid_size,
                "ask_size": self.ask_size,
                "quote_age_ms": quote_age_ms,
                "cls": method,
                "cond": cond,
            })

    def _on_bidask_tick(self, ticker_obj):
        """Called by ib_async when new BidAsk (quote) ticks arrive."""
        for tick in ticker_obj.tickByTicks:
            bid = getattr(tick, "bidPrice", None)
            ask = getattr(tick, "askPrice", None)
            bid_sz = getattr(tick, "bidSize", None)
            ask_sz = getattr(tick, "askSize", None)
            updated = False
            if bid and float(bid) > 0:
                self.bid = float(bid)
                updated = True
            if ask and float(ask) > 0:
                self.ask = float(ask)
                updated = True
            # Sizes can legitimately update even when the price is unchanged
            # (queue growing/shrinking) — capture them whenever present, and treat
            # a size-only change as an update too so OFI sees the queue dynamics.
            if bid_sz is not None and float(bid_sz) >= 0:
                self.bid_size = float(bid_sz)
                updated = True
            if ask_sz is not None and float(ask_sz) >= 0:
                self.ask_size = float(ask_sz)
                updated = True
            # Record when the NBBO last changed so _on_trade_tick can measure how
            # stale the quote was at classification time (quote-lag diagnostics),
            # and persist the quote to the raw_quotes stream for offline
            # time-aligned re-classification (merge_asof).
            if updated:
                t = getattr(tick, "time", None)
                qt = self._to_et(t) if t is not None else self.quote_time
                self.quote_time = qt
                if qt is not None and self.bid is not None and self.ask is not None:
                    self.quote_buffer.append({
                        "ticker": self.ticker,
                        "date": qt,
                        "bid": self.bid,
                        "ask": self.ask,
                        "bid_size": self.bid_size,
                        "ask_size": self.ask_size,
                    })
                    # Bound the buffer during quote-only periods (no trade to
                    # trigger the bar flush); persist and clear when it fills up.
                    if len(self.quote_buffer) >= QUOTE_FLUSH_LIMIT:
                        self._flush_quotes()

    # ── Bar flush ────────────────────────────────────────────────────────────

    def _flush_quotes(self):
        """Persist and clear the buffered NBBO stream (raw_quotes).

        Kept separate from _flush's bar logic so it runs independently of
        tick_buffer: a second that carried quotes but no trades must still
        persist its quotes, and the buffer must never grow without bound.
        """
        if not self.quote_buffer:
            return
        try:
            self.quote_col.insert_many(self.quote_buffer)
        except Exception as e:
            logging.error(f"MongoDB raw quote insert error: {e}")
        self.quote_buffer = []

    def _flush(self, second: datetime):
        """Aggregate tick_buffer into a 1-sec OHLCV bar and upsert to MongoDB."""
        # Flush the NBBO stream first — unconditionally, before the tick_buffer
        # early-return below, so quote-only seconds still persist their quotes.
        self._flush_quotes()

        if not self.tick_buffer:
            return

        # Bulk insert raw ticks
        if self.raw_buffer:
            try:
                self.raw_col.insert_many(self.raw_buffer)
            except Exception as e:
                logging.error(f"MongoDB raw tick insert error: {e}")
            self.raw_buffer = []

        prices = [t[0] for t in self.tick_buffer]
        sizes = [t[1] for t in self.tick_buffer]
        deltas = [t[2] for t in self.tick_buffer]

        bv = sum(d for d in deltas if d > 0)
        sv = sum(-d for d in deltas if d < 0)

        # OHLC from clean prices only (NBBO-band outliers excluded so a single bad
        # print can't blow out the wick); fall back to all prices if the whole
        # second was flagged. Volume/delta keep every tick (raw stays raw).
        ohlc_prices = [t[0] for t in self.tick_buffer if len(t) < 4 or t[3]] or prices
        n_dropped = len(prices) - len(ohlc_prices)
        if n_dropped:
            logging.info(
                f"[{second}] dropped {n_dropped} NBBO-outlier print(s) from OHLC "
                f"(bid={self.bid} ask={self.ask})"
            )

        bar = {
            "ticker":         self.ticker,
            "timeframe":      "1sec",
            "date":           second,
            "open":           ohlc_prices[0],
            "high":           max(ohlc_prices),
            "low":            min(ohlc_prices),
            "close":          ohlc_prices[-1],
            "volume":         sum(sizes),
            "buying_volume":  bv,
            "selling_volume": sv,
            "delta":          bv - sv,
            "source":         "ibkr_tick",
        }
        # Persist the distinct AllLast condition codes seen this second (if any),
        # so the closing-cross print can later be flagged by its exchange code.
        if self.cond_seen:
            bar["special_conditions"] = ",".join(sorted(self.cond_seen))

        try:
            self.col.update_one(
                {"ticker": self.ticker, "timeframe": "1sec", "date": second},
                {"$set": bar},
                upsert=True,
            )
            logging.debug(
                f"[{second}] O={bar['open']} C={bar['close']} "
                f"V={bar['volume']:.0f} Δ={bar['delta']:.0f} "
                f"(B={bv:.0f} S={sv:.0f})"
            )
        except Exception as e:
            logging.error(f"MongoDB upsert error for {second}: {e}")

    # ── Connect + subscribe (one session) ────────────────────────────────────

    async def _connect_and_subscribe(self):
        """Connect to the gateway and (re)subscribe both tick streams."""
        logging.info(
            f"[{self.ticker}] Connecting to IB Gateway "
            f"(port={self.port}, clientId={self.client_id})..."
        )
        await self.ib.connectAsync(IB_HOST, self.port,
                                   clientId=self.client_id,
                                   readonly=READONLY)
        logging.info(f"[{self.ticker}] Connected.")

        contract = Stock(self.ticker, "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)
        logging.info(f"[{self.ticker}] Contract qualified: {contract}")

        # Subscribe to trade ticks (AllLast = all last-trade prints)
        trade_ticker = self.ib.reqTickByTickData(
            contract, "AllLast", numberOfTicks=0, ignoreSize=False
        )
        trade_ticker.updateEvent += self._on_trade_tick

        # Subscribe to quote ticks for aggressor classification
        quote_ticker = self.ib.reqTickByTickData(
            contract, "BidAsk", numberOfTicks=0, ignoreSize=False
        )
        quote_ticker.updateEvent += self._on_bidask_tick

        logging.info(f"[{self.ticker}] Streaming tick data → MongoDB (1-sec bars).")

    # ── Main async loop ──────────────────────────────────────────────────────

    async def run(self):
        """Connect, stream, and RECONNECT on drops.

        The gateway drops the API connection on its daily/weekly restart. ib_async
        does NOT auto-resubscribe tick-by-tick streams, so we detect the drop
        (isConnected() goes False) and rebuild the session ourselves. Without this
        the process stays alive but silently collects nothing (a false heartbeat).
        """
        RECONNECT_DELAY = 30  # seconds between reconnect attempts
        try:
            while True:
                try:
                    await self._connect_and_subscribe()
                    # Hold the session open; break out the moment the socket drops.
                    while self.ib.isConnected():
                        await asyncio.sleep(15)
                    logging.warning(
                        f"[{self.ticker}] Connection LOST — flushing and reconnecting "
                        f"in {RECONNECT_DELAY}s..."
                    )
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except Exception as e:
                    logging.error(f"[{self.ticker}] Session error: {e!r} — retrying "
                                  f"in {RECONNECT_DELAY}s...")
                finally:
                    # Flush any partial second before the gap; disconnect cleanly so
                    # the clientId is freed before we reconnect with the same id.
                    if self.current_second is not None:
                        self._flush(self.current_second)
                        self.current_second = None
                    if self.ib.isConnected():
                        self.ib.disconnect()
                await asyncio.sleep(RECONNECT_DELAY)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            if self.current_second is not None:
                self._flush(self.current_second)
            if self.ib.isConnected():
                self.ib.disconnect()
            logging.info(f"[{self.ticker}] Stopped.")


# ─────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────

async def _run_all(tickers: list[str], port: int, base_client_id: int):
    tasks = [
        TickCollector(t, port=port, client_id=base_client_id + i).run()
        for i, t in enumerate(tickers)
    ]
    await asyncio.gather(*tasks)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    from ibkr.log_noise import install as _install_log_filter
    _install_log_filter()   # drop benign IBKR data-farm / account chatter
    parser = argparse.ArgumentParser(
        description="IBKR tick-by-tick collector → MongoDB 1-sec bars"
    )
    parser.add_argument(
        "--ticker", nargs="+", default=["NVDA"],
        help="Ticker(s) to collect (space-separated). Each gets a unique clientId.",
    )
    parser.add_argument(
        "--port", type=int, default=7497,
        help="IB Gateway port: TWS paper=7497, TWS live=7496, GW paper=4002, live=4001",
    )
    parser.add_argument(
        "--client-id", type=int, default=10, dest="client_id",
        help="Base clientId; each additional ticker gets +1 (default: 10)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Show per-bar debug output",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    asyncio.run(_run_all(args.ticker, args.port, args.client_id))


if __name__ == "__main__":
    main()
