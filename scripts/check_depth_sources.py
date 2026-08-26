#!/usr/bin/env python
"""
scripts/check_depth_sources.py
------------------------------
Answer one question: is NASDAQ TotalView actually feeding our Level-2 book, or
is the book being built from IEX alone?

Why this script exists
======================
Every depth subscription logs IBKR warning 2152, which reads:

    Exchanges - Depth: IEX; Top: BYX; AMEX; ... ;
    Need additional market data permissions - Depth: BATS; NASDAQ; ARCA; ...

That warning looks damning, but it is NOT evidence of a problem: it appeared
verbatim on 2026-07-22 while the books were confirmed full at 10x10. So the
warning cannot be used to decide anything, in either direction.

What CAN decide it is the per-row `marketMaker` field. In a SMART-aggregated
book every row carries the venue that contributed it. If every row says IEX,
TotalView is not reaching this session no matter what the subscription page
says. If rows say NSDQ, it is.

The script also requests the NATIVE NASDAQ book (exchange="NASDAQ",
isSmartDepth=False) as a cross-check, because that one CANNOT be served by
IEX — it either works, which proves the entitlement, or it errors.

Run during REGULAR HOURS (09:30-16:00 ET). Outside them the venue mix is not
the one you are asking about: overnight the book fills from OVERNIGHT and
IBEOS with NASDAQ and IEX both shut, so the answer is neither yes nor no.

    python scripts/check_depth_sources.py --ticker NVDA --port 7497
"""

import argparse
import asyncio
import logging
from collections import Counter

from ib_async import IB, Stock

# Own clientId so this never collides with a running collector (40/41) or the
# app's backfill (11).
CLIENT_ID = 77
SETTLE_SEC = 6.0


def _describe(book, label: str) -> None:
    bids = [r for r in (book.domBids or []) if r]
    asks = [r for r in (book.domAsks or []) if r]
    print(f"\n── {label} ─────────────────────────────────")
    if not bids and not asks:
        print("  EMPTY — no rows arrived.")
        return
    print(f"  rows: {len(bids)} bids x {len(asks)} asks")
    print(f"  distinct bid prices: {len({r.price for r in bids})}")
    venues = Counter(r.marketMaker or "?" for r in bids + asks)
    print(f"  contributing venues: {dict(venues)}")
    for r in bids[:5]:
        print(f"    bid {r.price:>10.2f} x {r.size:<10} {r.marketMaker}")


async def main_async(ticker: str, host: str, port: int) -> None:
    ib = IB()
    await ib.connectAsync(host, port, clientId=CLIENT_ID, readonly=True)
    try:
        smart = Stock(ticker, "SMART", "USD")
        await ib.qualifyContractsAsync(smart)
        smart_book = ib.reqMktDepth(smart, numRows=10, isSmartDepth=True)

        # The native NASDAQ book is requested only to see whether IBKR ACCEPTS
        # it — the answer arrives as an async error (10089) rather than an
        # exception, so watch the error stream rather than this call. Its
        # Ticker is deliberately not printed: ib_async keys tickers by conId,
        # so the same contract hands back the SAME object as the SMART request
        # and printing it would show the SMART book twice under two headings.
        native_refusal = []
        ib.errorEvent += lambda reqId, code, msg, c=None: (
            native_refusal.append(f"{code}: {msg}") if code in (10089, 309, 2152)
            and "DEEP" in str(msg) else None)
        native = Stock(ticker, "NASDAQ", "USD")
        try:
            await ib.qualifyContractsAsync(native)
            ib.reqMktDepth(native, numRows=10, isSmartDepth=False)
        except Exception as e:
            native_refusal.append(repr(e))

        await asyncio.sleep(SETTLE_SEC)

        _describe(smart_book, f"{ticker} SMART book (isSmartDepth=True)")

        print(f"\n── {ticker} native NASDAQ deep book (TotalView) ──────────────")
        if native_refusal:
            for m in dict.fromkeys(native_refusal):
                print(f"  REFUSED  {m}")
            print("  -> TotalView is NOT entitled on this login.")
        else:
            print("  accepted — TotalView is entitled on this login.")

        print("\nHow to read this (regular hours only):")
        print("  NSDQ among the venues       -> TotalView is feeding.")
        print("  native book refused         -> TotalView is not on THIS login.")
        print("                                 On a paper account, check 'Share")
        print("                                 real-time market data with paper")
        print("                                 trading account' in Client Portal.")
        print("  only OVERNIGHT/IBEOS        -> you ran this outside regular")
        print("                                 hours. Re-run 09:30-16:00 ET.")
        print("\nNote: the SMART book can be full (10x10) while TotalView is off —")
        print("the other venues supply the rows. A full book is NOT evidence of it.")

        # Cancel what we opened. Depth lines are a limited resource (3 for a
        # retail account) and a leaked one starves the running collector.
        for c, sm in ((smart, True), (native, False)):
            try:
                ib.cancelMktDepth(c, isSmartDepth=sm)
            except Exception:
                pass
    finally:
        ib.disconnect()


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", default="NVDA")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7497,
                    help="Gateway paper 4002 (4004 in the container), TWS paper 7497")
    args = ap.parse_args()
    asyncio.run(main_async(args.ticker.upper(), args.host, args.port))


if __name__ == "__main__":
    main()
