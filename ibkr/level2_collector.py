"""
ibkr/level2_collector.py
------------------------
IBKR Level-2 (market depth / DOM) collector.

Polls reqMktDepth books and stores a snapshot per ticker every --interval
seconds into trading_cvd.level2_snapshots, in the exact document shape the
mock stream (tests/mock_level2_stream.py) produces — the heatmap/S&R code
cannot tell them apart except by the src field ('ibkr' vs 'mock').

Snapshot timestamps use the naive-ET-as-UTC epoch convention shared with the
candle store (see level2_webapp.data_provider.et_epoch) — a previous version
of this file wrote real UTC epochs (time.time()), which land 4-5 hours off
the candles and silently break the heatmap time matching.

REQUIREMENTS
  - A market-depth subscription on the IBKR account (e.g. NASDAQ TotalView).
    Without it reqMktDepth fails (error 309 / "not subscribed"); the collector
    logs the failure per ticker and keeps running the others.
  - IB Gateway / TWS on --port (default 7497).

NOT yet run against a live gateway (written while the account was locked);
the connection/reconnect scaffolding mirrors ibkr/tick_collector.py which is
battle-tested. First live run: start with one ticker and watch the log for
"Subscribed depth".

clientId 63 (taken: research 40-42, user backfill 11, dynamic 60/61, one-off 62).

Usage:
  python -m ibkr.level2_collector --tickers NVDA --depth 10          # static list
  python -m ibkr.level2_collector --tickers NVDA SOFI --no-smart
  python -m ibkr.level2_collector --dynamic --max 3                  # follow the app

In --dynamic mode the collector has no fixed list: it watches the same
collector_requests queue the dash app writes on ticker view and keeps depth
subscribed for the most-recently-viewed tickers (up to --max, because IBKR caps
concurrent depth lines). Search a ticker in the app and its book starts within a
few seconds; the least-recently-viewed one is dropped when the cap is hit.
"""

import argparse
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ib_async import IB, Stock

from level2_webapp.data_provider import (
    get_l2_collection, snapshot_doc, ensure_l2_indexes,
)
from history.schema import mongo_client, DB_NAME

ET = ZoneInfo("America/New_York")

# The machine running IB Gateway / TWS. Loopback is right for every local run
# and stays the default; a container deploy is the case that needs it
# overridden, because there Gateway is a separate service and 127.0.0.1 is this
# process's own container. Set IB_HOST to the Gateway service name.
IB_HOST = os.environ.get("IB_HOST", "127.0.0.1")

# Dynamic mode: how often to reconcile the subscribed set with collector_requests.
DYNAMIC_POLL_SEC = 5.0

# Only collector_requests seen this recently are candidates for a depth line, so
# a stale/partial symbol left in the shared queue never churns a depth slot.
REQUEST_TTL_SEC = 1800.0

# Staleness watchdog: during regular hours a live depth line produces a snapshot
# every `interval` seconds. If a line that WAS producing goes silent this long
# while the socket still looks connected — a half-dead connection or a depth line
# the gateway quietly dropped after a restart (the observed failure mode) — force
# a reconnect + resubscribe. Guarded to RTH and to lines that have already
# captured at least once, so a legitimately empty overnight/weekend book never
# thrashes the connection.
STALE_RECONNECT_SEC = 120.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
from ibkr.log_noise import install as _install_log_filter
_install_log_filter()   # drop benign IBKR data-farm / account chatter
log = logging.getLogger("level2_collector")


class Level2Collector:
    def __init__(self, tickers: list[str], port: int = 7497, client_id: int = 63,
                 depth: int = 10, interval: float = 0.5, smart: bool = True,
                 rth_only: bool = False, dynamic: bool = False, max_tickers: int = 3):
        self.symbols = [t.upper() for t in tickers]
        self.port = port
        self.client_id = client_id
        self.depth = depth
        self.interval = interval
        self.smart = smart          # isSmartDepth=True aggregates across exchanges
        self.rth_only = rth_only    # collect 09:30-16:00 ET only (Eric-style)
        # Dynamic mode: follow whatever tickers the app is showing (it writes
        # collector_requests on view) instead of a fixed list, subject to an LRU
        # cap because IBKR limits concurrent market-depth lines (typically 3).
        self.dynamic = dynamic
        self.max_tickers = max_tickers
        self.ib = IB()
        self.books = {}             # symbol -> ib_async Ticker with domBids/domAsks
        self.contracts = {}         # symbol -> qualified Contract (needed to cancel)
        self.col = get_l2_collection()
        ensure_l2_indexes(self.col)
        self.req_col = mongo_client()[DB_NAME]["collector_requests"] if dynamic else None
        self._batch = []            # buffered snapshot docs
        self._last_flush = 0.0
        self._last_reconcile = 0.0
        self._last_capture = 0.0    # monotonic: last time any book yielded a snapshot
        self._captured = False      # any depth captured since the current connect?

    def _desired(self) -> list[str]:
        """Most-recently-requested tickers (top max_tickers), from the same
        collector_requests queue the app writes on ticker view."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=REQUEST_TTL_SEC)
        out = []
        for d in self.req_col.find({"last_requested": {"$gte": cutoff}},
                                   {"_id": 1}).sort("last_requested", -1):
            out.append(str(d["_id"]).upper())
            if len(out) >= self.max_tickers:
                break
        return out

    async def _subscribe_one(self, sym: str) -> bool:
        if sym in self.books:
            return True
        try:
            contract = Stock(sym, "SMART", "USD")
            await self.ib.qualifyContractsAsync(contract)
            self.books[sym] = self.ib.reqMktDepth(
                contract, numRows=self.depth, isSmartDepth=self.smart)
            self.contracts[sym] = contract
            log.info(f"Subscribed depth: {sym} ({len(self.books)}/{self.max_tickers} lines)")
            return True
        except Exception as e:
            # Typical without a depth subscription: error 309/2152.
            log.error(f"Depth subscribe failed for {sym}: {e} — skipping this ticker")
            return False

    def _cancel_one(self, sym: str, reason: str = "evicted"):
        contract = self.contracts.pop(sym, None)
        self.books.pop(sym, None)
        if contract is not None:
            try:
                self.ib.cancelMktDepth(contract, isSmartDepth=self.smart)
            except Exception as e:
                log.debug(f"cancelMktDepth failed for {sym}: {e}")
        log.info(f"Unsubscribed depth: {sym} ({reason})")

    async def _reconcile(self):
        """Dynamic mode: match subscribed books to the desired (LRU) set."""
        desired = self._desired()
        for sym in [s for s in self.books if s not in desired]:
            self._cancel_one(sym)
        for sym in desired:
            if sym not in self.books:
                await self._subscribe_one(sym)

    async def _connect_and_subscribe(self):
        await self.ib.connectAsync(IB_HOST, self.port, clientId=self.client_id)
        log.info(f"Connected (clientId={self.client_id})")
        self.books = {}
        self.contracts = {}
        targets = self._desired() if self.dynamic else self.symbols
        for sym in targets:
            await self._subscribe_one(sym)
        if not self.dynamic and not self.books:
            raise RuntimeError("No depth subscription succeeded — check market-data subscriptions")
        if self.dynamic and not self.books:
            log.info("Dynamic mode: no tickers requested yet — waiting for the app.")

    @staticmethod
    def _side(levels) -> list:
        out = []
        for lvl in levels or []:
            try:
                price, size = float(lvl.price), float(lvl.size)
            except (TypeError, ValueError):
                continue
            if price > 0 and size > 0:
                out.append({"price": price, "size": size})
        return out

    @staticmethod
    def _is_rth(now_et) -> bool:
        """Regular US trading hours 09:30–16:00 ET (weekday). Used both by
        --rth-only and by the staleness watchdog (which must not fire when an
        empty book is legitimate — nights, weekends, holidays)."""
        if now_et.weekday() >= 5:      # Sat/Sun
            return False
        m = now_et.hour * 60 + now_et.minute
        return 9 * 60 + 30 <= m < 16 * 60

    def _snapshot_all(self) -> int:
        now_et = datetime.now(ET).replace(tzinfo=None)
        if self.rth_only and not self._is_rth(now_et):
            return 0
        n = 0
        for sym, book in self.books.items():
            bids = self._side(getattr(book, "domBids", None))
            asks = self._side(getattr(book, "domAsks", None))
            if not bids and not asks:
                continue   # book not populated yet (or outside hours)
            self._batch.append(snapshot_doc(sym, now_et, bids, asks, src="ibkr"))
            n += 1
        # Batch inserts (Eric-style): one Mongo round-trip per ~5s / 20 docs
        # instead of per snapshot.
        import time as _time
        now = _time.monotonic()
        if self._batch and (len(self._batch) >= 20 or now - self._last_flush >= 5.0):
            self.col.insert_many(self._batch, ordered=False)
            self._batch = []
            self._last_flush = now
        if n:
            self._last_capture = now
            self._captured = True
        return n

    async def run(self):
        n_written = 0
        while True:
            try:
                await self._connect_and_subscribe()
                self._captured = False                  # fresh connect: watchdog waits
                self._last_capture = time.monotonic()   # for a first real capture
                last_log = 0.0
                while self.ib.isConnected():
                    await asyncio.sleep(self.interval)
                    # Dynamic mode: periodically follow the app's viewed tickers.
                    if self.dynamic and time.monotonic() - self._last_reconcile >= DYNAMIC_POLL_SEC:
                        self._last_reconcile = time.monotonic()
                        await self._reconcile()
                    n_written += self._snapshot_all()

                    # Staleness watchdog — reconnect a silently-dead RTH line.
                    mono = time.monotonic()
                    if (self.books and self._captured
                            and self._is_rth(datetime.now(ET).replace(tzinfo=None))
                            and mono - self._last_capture > STALE_RECONNECT_SEC):
                        log.warning(
                            f"No depth captured for {mono - self._last_capture:.0f}s "
                            f"during RTH while connected — forcing reconnect/resubscribe")
                        break   # → finally disconnects → outer loop reconnects + resubscribes

                    now = asyncio.get_event_loop().time()
                    if now - last_log > 60:
                        books = {s: f"{len(getattr(b, 'domBids', None) or [])}x"
                                    f"{len(getattr(b, 'domAsks', None) or [])}"
                                 for s, b in self.books.items()}
                        log.info(f"depth alive: total={n_written} "
                                 f"last_capture={mono - self._last_capture:.0f}s ago books={books}")
                        last_log = now
                log.warning("Disconnected — reconnecting in 10s")
            except (KeyboardInterrupt, asyncio.CancelledError):
                log.info("Stopping")
                break
            except Exception as e:
                log.error(f"Collector error: {e} — retrying in 10s")
            finally:
                try:
                    self.ib.disconnect()
                except Exception:
                    pass
            await asyncio.sleep(10)


def main():
    ap = argparse.ArgumentParser(description="IBKR Level-2 depth snapshot collector")
    ap.add_argument("--tickers", nargs="+", default=[],
                    help="fixed ticker list (static mode); omit when using --dynamic")
    ap.add_argument("--dynamic", action="store_true",
                    help="follow the app's viewed tickers via collector_requests (LRU)")
    ap.add_argument("--max", type=int, default=3,
                    help="dynamic mode: max concurrent depth lines (default 3, IBKR line limit)")
    ap.add_argument("--port", type=int, default=7497)
    ap.add_argument("--client-id", type=int, default=63)
    ap.add_argument("--depth", type=int, default=10, help="book rows per side")
    ap.add_argument("--interval", type=float, default=0.5, help="seconds between snapshots")
    ap.add_argument("--no-smart", action="store_true",
                    help="single-exchange depth instead of SMART aggregated")
    ap.add_argument("--rth-only", action="store_true",
                    help="collect regular trading hours (09:30-16:00 ET) only")
    args = ap.parse_args()

    if not args.dynamic and not args.tickers:
        ap.error("provide --tickers, or use --dynamic")

    collector = Level2Collector(
        tickers=args.tickers, port=args.port, client_id=args.client_id,
        depth=args.depth, interval=args.interval, smart=not args.no_smart,
        rth_only=args.rth_only, dynamic=args.dynamic, max_tickers=args.max)
    asyncio.run(collector.run())


if __name__ == "__main__":
    main()
