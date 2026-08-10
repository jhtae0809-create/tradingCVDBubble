"""
ibkr/dynamic_collector.py — the single, unified IBKR collector.

ONE process, ONE IB connection. Run this plus the Dash app; nothing else.
It follows what the user does in the app: the app upserts a `collector_requests`
row whenever a ticker is viewed, and this collector keeps live data flowing for
the most-recently-requested tickers.

What it collects, per active ticker:
  * tick-by-tick trades + quotes  → 1-sec bars (reuses TickCollector's
    buffering / classification / flush logic).
  * a one-time catch-up 1-sec backfill on first subscription (fills the gap
    between the latest stored bar and now, capped at --backfill-hours).
  * Level-2 market depth snapshots → level2_snapshots (for the heatmap / S&R),
    for the top few active tickers (IBKR caps concurrent depth lines).
  * a periodic FinViz i1 refresh + rollup (the consolidated-volume reference
    that scales the thin tick-stream volume up to real traded volume).

Priority / slots:
  * Pure on-demand queue: the tick slots (up to --max, default 5) are filled by
    the tickers you search in the app, most-recently-viewed first; the
    least-recently-viewed one is evicted when a newer request needs the slot.
  * Depth follows the top --max-depth (default 3, the IBKR line limit) of that
    same ordered set.
  * --pin can still force a ticker to always hold a slot, but the default is
    empty — nothing is collected until you search for it.

One IB connection carries all tick-by-tick AND depth subscriptions (they are
per-contract, independent line pools); a short-lived clientId+1 connection is
used only for catch-up backfills.

Usage:
    python -m ibkr.dynamic_collector                       # defaults below
    python -m ibkr.dynamic_collector --max 5 --max-depth 3 # pure on-demand
    python -m ibkr.dynamic_collector --pin NVDA            # force one always-on
    python -m ibkr.dynamic_collector --no-l2               # ticks only
"""

import argparse
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pymongo import MongoClient
from ib_async import IB, Stock

from ibkr.tick_collector import TickCollector, MONGO_URI, DB_NAME
from ibkr.backfill import backfill_ticker
from level2_webapp.data_provider import (
    get_l2_collection, snapshot_doc, ensure_l2_indexes,
)

ET = ZoneInfo("America/New_York")

POLL_SEC = 5                 # how often to reconcile subscriptions with requests
REQUEST_TTL_SEC = 1800       # only requests seen this recently are LRU candidates
FAIL_COOLDOWN_SEC = 300      # don't retry a ticker that failed to subscribe for this long
RECONNECT_DELAY = 30
MIN_BACKFILL_GAP_SEC = 120   # skip catch-up backfill for gaps smaller than this
FINVIZ_REFRESH_SEC = 600     # consolidated-volume reference refresh cadence

# Depth (Level-2) snapshot cadence + staleness watchdog (see level2_collector).
DEPTH_INTERVAL_SEC = 0.5
DEPTH_STALE_RECONNECT_SEC = 120.0

# Every IB API port, in the order we probe them. IB Gateway and TWS speak the
# IDENTICAL API — the port is the only difference — so dialling the wrong one
# does not fail loudly, it just means the collector connects to nothing and
# silently never collects. That made "I ran it and nothing happened" the most
# common live-mode failure, since the old default (7497) is TWS paper while
# IB Gateway defaults to 4002. With no --port we try all four.
# Labels name each port's DEFAULT owner, not what is necessarily listening:
# either application can be pointed at any port, so a Gateway configured on 7497
# is perfectly normal. Probing is what settles it.
IB_PORTS = [
    (7497, "TWS paper default"),
    (4002, "IB Gateway paper default"),
    (7496, "TWS live default"),
    (4001, "IB Gateway live default"),
]
CONNECT_PROBE_TIMEOUT = 5.0     # per-port probe; a closed port refuses instantly


class UnifiedCollector:
    def __init__(self, port: int | None, client_id: int, max_tickers: int, max_depth: int,
                 pinned: list[str], backfill_hours: float, collect_l2: bool = True,
                 depth_rows: int = 10, depth_interval: float = DEPTH_INTERVAL_SEC,
                 smart_depth: bool = True):
        # port=None means "auto-detect" (see IB_PORTS / _connect_any). Once a
        # port answers it is stored here, so the tick collectors and backfill
        # connections spawned later all dial the same endpoint.
        self.auto_port = port is None
        self.port = port if port is not None else IB_PORTS[0][0]
        self._last_good_port: int | None = None
        self.client_id = client_id
        self.max_tickers = max_tickers
        self.max_depth = max_depth
        # Pinned set keeps insertion order = priority order.
        self.pinned = [t.upper() for t in pinned]
        self.backfill_hours = backfill_hours
        self.collect_l2 = collect_l2
        self.depth_rows = depth_rows
        self.depth_interval = depth_interval
        self.smart_depth = smart_depth

        self.ib = IB()
        mongo = MongoClient(MONGO_URI)
        self.req_col = mongo[DB_NAME]["collector_requests"]
        self.candles = mongo[DB_NAME]["candles"]

        # ticker -> {"collector": TickCollector, "contract": Stock}
        self.active: dict[str, dict] = {}
        # ticker -> {"book": Ticker, "contract": Stock}  (Level-2 depth)
        self.books: dict[str, dict] = {}
        self.failed_until: dict[str, float] = {}
        self.backfill_queue: asyncio.Queue[str] = asyncio.Queue()
        self.backfilled: set[str] = set()

        # Depth snapshot buffering + watchdog state.
        self.col = get_l2_collection() if collect_l2 else None
        if self.col is not None:
            ensure_l2_indexes(self.col)
        self._batch: list = []
        self._last_flush = 0.0
        self._last_reconcile = 0.0
        self._last_capture = 0.0
        self._captured = False

    # ── Request → desired sets ───────────────────────────────────────────────

    def _recent_requests(self) -> list[str]:
        """Requested tickers within REQUEST_TTL_SEC, most-recent first."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=REQUEST_TTL_SEC)
        out = []
        for d in self.req_col.find({"last_requested": {"$gte": cutoff}},
                                   {"_id": 1}).sort("last_requested", -1):
            out.append(str(d["_id"]).upper())
        return out

    def _desired_ticks(self) -> list[str]:
        """Pinned tickers first (fixed priority), then the most recently
        requested, deduped, capped at max_tickers. Cooled-down failures are
        skipped (so a bad symbol frees its slot for a live one)."""
        now = time.time()
        ordered = list(self.pinned)
        for t in self._recent_requests():
            if t not in ordered:
                ordered.append(t)
        out = []
        for t in ordered:
            if self.failed_until.get(t, 0) > now:
                continue
            out.append(t)
            if len(out) >= self.max_tickers:
                break
        return out

    def _desired_depth(self) -> list[str]:
        """Depth follows the top max_depth of the tick set (pinned first)."""
        if not self.collect_l2:
            return []
        return self._desired_ticks()[:self.max_depth]

    # ── Tick subscribe / unsubscribe ─────────────────────────────────────────

    async def _sub_ticks(self, ticker: str):
        # TickCollector is used purely for its buffering/flush/classification
        # state; its own self.ib is never connected here.
        collector = TickCollector(ticker, port=self.port, client_id=self.client_id)
        contract = Stock(ticker, "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)

        trade_tk = self.ib.reqTickByTickData(contract, "AllLast", numberOfTicks=0, ignoreSize=False)
        trade_tk.updateEvent += collector._on_trade_tick
        quote_tk = self.ib.reqTickByTickData(contract, "BidAsk", numberOfTicks=0, ignoreSize=False)
        quote_tk.updateEvent += collector._on_bidask_tick

        self.active[ticker] = {"collector": collector, "contract": contract}
        logging.info(f"[col] SUBSCRIBED ticks {ticker} "
                     f"({len(self.active)}/{self.max_tickers} slots)")

        if ticker not in self.backfilled:
            self.backfilled.add(ticker)
            await self.backfill_queue.put(ticker)

    def _unsub_ticks(self, ticker: str, reason: str = "evicted"):
        st = self.active.pop(ticker, None)
        if st is None:
            return
        try:
            self.ib.cancelTickByTickData(st["contract"], "AllLast")
            self.ib.cancelTickByTickData(st["contract"], "BidAsk")
        except Exception:
            pass  # already disconnected
        c = st["collector"]
        try:
            if c.current_second is not None:
                c._flush(c.current_second)
                c.current_second = None
            else:
                c._flush_quotes()
        except Exception as e:
            logging.error(f"[col] flush on unsubscribe failed for {ticker}: {e}")
        logging.info(f"[col] unsubscribed ticks {ticker} ({reason})")

    # ── Depth subscribe / cancel (Level-2) ───────────────────────────────────

    async def _sub_depth(self, ticker: str):
        contract = Stock(ticker, "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)
        book = self.ib.reqMktDepth(contract, numRows=self.depth_rows,
                                   isSmartDepth=self.smart_depth)
        self.books[ticker] = {"book": book, "contract": contract}
        logging.info(f"[col] subscribed depth {ticker} "
                     f"({len(self.books)}/{self.max_depth} lines)")

    def _cancel_depth(self, ticker: str, reason: str = "evicted"):
        st = self.books.pop(ticker, None)
        if st is None:
            return
        try:
            self.ib.cancelMktDepth(st["contract"], isSmartDepth=self.smart_depth)
        except Exception as e:
            logging.debug(f"[col] cancelMktDepth failed for {ticker}: {e}")
        logging.info(f"[col] unsubscribed depth {ticker} ({reason})")

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
        if now_et.weekday() >= 5:
            return False
        m = now_et.hour * 60 + now_et.minute
        return 9 * 60 + 30 <= m < 16 * 60

    def _snapshot_depth(self) -> int:
        """Capture a depth snapshot per subscribed book into level2_snapshots."""
        if not self.collect_l2 or not self.books:
            return 0
        now_et = datetime.now(ET).replace(tzinfo=None)
        n = 0
        for sym, st in self.books.items():
            book = st["book"]
            bids = self._side(getattr(book, "domBids", None))
            asks = self._side(getattr(book, "domAsks", None))
            if not bids and not asks:
                continue
            self._batch.append(snapshot_doc(sym, now_et, bids, asks, src="ibkr"))
            n += 1
        mono = time.monotonic()
        if self._batch and (len(self._batch) >= 20 or mono - self._last_flush >= 5.0):
            self.col.insert_many(self._batch, ordered=False)
            self._batch = []
            self._last_flush = mono
        if n:
            self._last_capture = mono
            self._captured = True
        return n

    # ── Reconcile subscriptions with the desired sets ────────────────────────

    async def _reconcile(self):
        desired = self._desired_ticks()
        for t in [t for t in self.active if t not in desired]:
            self._unsub_ticks(t)
        for t in desired:
            if t in self.active:
                continue
            try:
                await self._sub_ticks(t)
            except Exception as e:
                logging.error(f"[col] subscribe ticks {t} failed: {e!r} — cooldown "
                              f"{FAIL_COOLDOWN_SEC}s")
                self.failed_until[t] = time.time() + FAIL_COOLDOWN_SEC
                self._unsub_ticks(t, reason="subscribe failed")

        if self.collect_l2:
            depth = self._desired_depth()
            for s in [s for s in self.books if s not in depth]:
                self._cancel_depth(s)
            for s in depth:
                if s in self.books:
                    continue
                try:
                    await self._sub_depth(s)
                except Exception as e:
                    # Typical without a depth subscription (TotalView): error
                    # 309/2152. Skip this ticker's depth; ticks still flow.
                    logging.warning(f"[col] depth subscribe {s} failed: {e!r} "
                                    f"— ticks unaffected")

    # ── Catch-up backfill worker (sequential, own clientId) ──────────────────

    async def _backfill_worker(self):
        while True:
            ticker = await self.backfill_queue.get()
            try:
                now = datetime.now(ET).replace(tzinfo=None)
                start = now - timedelta(hours=self.backfill_hours)
                last = self.candles.find_one(
                    {"ticker": ticker, "timeframe": "1sec"}, sort=[("date", -1)])
                if last and last["date"] > start:
                    start = last["date"]
                if (now - start).total_seconds() < MIN_BACKFILL_GAP_SEC:
                    continue
                logging.info(f"[col] catch-up 1sec backfill {ticker}: {start} → {now}")
                # resume=False: backfill_meta coverage is a start/end UNION, so
                # after any earlier backfill it "covers" the very gap we're here
                # to fill and the resume logic would skip it. Upserts are
                # idempotent, so refetching is safe.
                await backfill_ticker(
                    ticker, start, now, barsize="1sec",
                    port=self.port, client_id=self.client_id + 1,
                    resume=False,
                )
            except Exception as e:
                logging.error(f"[col] backfill {ticker} failed: {e!r}")

    # ── FinViz i1 refresher ──────────────────────────────────────────────────
    # Every collected ticker needs a fresh FinViz i1 series: it is the
    # consolidated-volume reference history.rollup.scale_tick_volume uses to
    # scale thin tick-stream volume (~10% of tape) up to real traded volume.
    # The Dash app only fetches the ticker being VIEWED, so without this,
    # collected-but-unviewed tickers (e.g. a pinned SOFI) accumulate unscalable
    # tick buckets.

    async def _finviz_refresh_worker(self):
        while True:
            for t in sorted(set(self.active) | set(self.pinned)):
                try:
                    from finviz.new_finviz import fetch_and_save
                    from history.rollup import rollup_ticker
                    await asyncio.to_thread(fetch_and_save, t, "i1")
                    stats = await asyncio.to_thread(rollup_ticker, t)
                    written = {k: v for k, v in stats.items() if v}
                    if written:
                        logging.info(f"[col] i1 refresh + rollup {t}: {written}")
                except Exception as e:
                    logging.warning(f"[col] i1 refresh {t} failed: {e!r}")
                await asyncio.sleep(3)   # be polite to FinViz
            await asyncio.sleep(FINVIZ_REFRESH_SEC)

    # ── Connection ───────────────────────────────────────────────────────────

    async def _connect_any(self):
        """Connect to whichever IB endpoint is actually listening.

        With an explicit --port we dial only that one, so a deliberate choice is
        never silently overridden. Otherwise we walk IB_PORTS until one answers,
        trying the last known-good port first so reconnects are immediate. The
        chosen port is pinned to self.port because TickCollector and the backfill
        worker open their own connections and must reach the same Gateway/TWS.
        """
        if not self.auto_port:
            candidates = [(self.port, "explicit --port")]
        else:
            candidates = sorted(
                IB_PORTS, key=lambda pl: pl[0] != self._last_good_port)

        errors = []
        for port, label in candidates:
            try:
                logging.info(f"[col] connecting to IB on port {port} ({label}), "
                             f"clientId={self.client_id}...")
                await self.ib.connectAsync("127.0.0.1", port,
                                           clientId=self.client_id,
                                           timeout=CONNECT_PROBE_TIMEOUT)
            except Exception as e:
                errors.append(f"{port} ({label}): {e!r}")
                continue

            if self._last_good_port != port:
                logging.info(f"[col] connected on port {port} ({label}) — tick "
                             f"and backfill connections will use it too")
            self.port = port
            self._last_good_port = port
            return

        # Nothing answered. This is the failure people hit most often, so spell
        # out every cause rather than leaving a bare connection refusal.
        detail = "\n  ".join(errors)
        logging.error(
            "[col] could not reach IB on any known API port.\n"
            f"  {detail}\n"
            "  Check, in order:\n"
            "    1. IB Gateway or TWS is running AND logged in.\n"
            "    2. API is enabled: Configuration > Settings > API > Settings >\n"
            "       'Enable ActiveX and Socket Clients'.\n"
            "    3. The API port there matches one of "
            f"{', '.join(str(p) for p, _ in IB_PORTS)} "
            "(Gateway paper is 4002, TWS paper 7497).\n"
            "    4. 127.0.0.1 is in the Trusted IPs list.\n"
            "  Force a specific port with:  python -m ibkr.dynamic_collector --port <n>"
        )
        raise ConnectionError("no IB API port answered")

    # ── Main loop with reconnect ─────────────────────────────────────────────

    async def run(self):
        try:
            while True:
                try:
                    await self._connect_any()
                    logging.info(f"[col] connected — pinned={self.pinned} "
                                 f"max_ticks={self.max_tickers} max_depth="
                                 f"{self.max_depth if self.collect_l2 else 0}")
                    self._captured = False
                    self._last_capture = time.monotonic()
                    self._last_reconcile = 0.0   # reconcile immediately
                    last_log = 0.0
                    while self.ib.isConnected():
                        await asyncio.sleep(self.depth_interval)
                        mono = time.monotonic()
                        if mono - self._last_reconcile >= POLL_SEC:
                            self._last_reconcile = mono
                            await self._reconcile()
                        self._snapshot_depth()

                        # Depth staleness watchdog — reconnect a silently-dead
                        # RTH line (half-open socket / gateway-dropped line).
                        if (self.collect_l2 and self.books and self._captured
                                and self._is_rth(datetime.now(ET).replace(tzinfo=None))
                                and mono - self._last_capture > DEPTH_STALE_RECONNECT_SEC):
                            logging.warning(
                                f"[col] no depth for {mono - self._last_capture:.0f}s "
                                f"during RTH — forcing reconnect/resubscribe")
                            break
                        if mono - last_log > 60:
                            logging.info(f"[col] alive: ticks={sorted(self.active)} "
                                         f"depth={sorted(self.books)}")
                            last_log = mono
                    logging.warning(f"[col] connection LOST — reconnecting in "
                                    f"{RECONNECT_DELAY}s")
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except Exception as e:
                    logging.error(f"[col] session error: {e!r} — retrying in "
                                  f"{RECONNECT_DELAY}s")
                finally:
                    for t in list(self.active):
                        self._unsub_ticks(t, reason="session ended")
                    for s in list(self.books):
                        self._cancel_depth(s, reason="session ended")
                    if self.ib.isConnected():
                        self.ib.disconnect()
                await asyncio.sleep(RECONNECT_DELAY)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            for t in list(self.active):
                self._unsub_ticks(t, reason="shutdown")
            for s in list(self.books):
                self._cancel_depth(s, reason="shutdown")
            if self.ib.isConnected():
                self.ib.disconnect()
            logging.info("[col] stopped.")


async def _main_async(args):
    mgr = UnifiedCollector(
        port=args.port, client_id=args.client_id, max_tickers=args.max,
        max_depth=args.max_depth, pinned=args.pin, backfill_hours=args.backfill_hours,
        collect_l2=not args.no_l2, depth_rows=args.depth, smart_depth=not args.no_smart,
    )
    await asyncio.gather(mgr.run(), mgr._backfill_worker(),
                         mgr._finviz_refresh_worker())


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    from ibkr.log_noise import install as _install_log_filter
    _install_log_filter()   # drop benign IBKR data-farm / account chatter
    parser = argparse.ArgumentParser(
        description="Unified IBKR collector (ticks + 1sec backfill + L2 depth), "
                    "driven by app ticker searches.")
    parser.add_argument(
        "--port", type=int, default=None,
        help="IB API port. Omit to auto-detect by trying, in order: "
             + ", ".join(f"{p} ({label})" for p, label in IB_PORTS)
             + ". Gateway and TWS speak the same API; only the port differs.")
    parser.add_argument("--client-id", type=int, default=40, dest="client_id",
                        help="clientId for the streaming connection; +1 is used "
                             "for catch-up backfills (default: 40)")
    parser.add_argument("--max", type=int, default=5,
                        help="max simultaneously tick-subscribed tickers (default: 5)")
    parser.add_argument("--max-depth", type=int, default=3, dest="max_depth",
                        help="max concurrent L2 depth lines (default: 3, IBKR limit)")
    parser.add_argument("--pin", nargs="*", default=[],
                        help="always-collected tickers that hold the first "
                             "slots (default: none — pure on-demand queue driven "
                             "by what you search in the app).")
    parser.add_argument("--backfill-hours", type=float, default=24,
                        dest="backfill_hours",
                        help="max catch-up 1sec backfill span on first "
                             "subscription (default: 24)")
    parser.add_argument("--no-l2", action="store_true",
                        help="disable Level-2 depth collection (ticks only)")
    parser.add_argument("--no-smart", action="store_true",
                        help="single-exchange depth instead of SMART aggregated")
    parser.add_argument("--depth", type=int, default=20,
                        help="L2 book rows per side to request from IBKR "
                             "(default: 20 for a deeper, Bookmap-style heatmap; "
                             "IBKR SMART depth may cap the number actually "
                             "returned — see logs '[col] subscribed depth').")
    args = parser.parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
