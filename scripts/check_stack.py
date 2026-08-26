#!/usr/bin/env python
"""
scripts/check_stack.py
----------------------
Is live data actually flowing right now? Prints a verdict, not a data dump.

The point of sampling TWICE is that a nonzero count proves nothing — the
database is full of backfilled history, which sits there looking healthy while
the live stream is dead. Only a count that RISES between two samples shows the
tape is moving. Everything here is therefore a delta.

Run during regular hours (09:30-16:00 ET). Outside them zero ticks is correct
behaviour, not a fault, and the script says so rather than reporting failure.

    # containers  (compose publishes mongo on 27018 to avoid the local 27017)
    python scripts/check_stack.py --mongo mongodb://127.0.0.1:27018/

    # running natively on the Mac
    python scripts/check_stack.py
"""

import argparse
import os
import time
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient
from pymongo.errors import PyMongoError

DB_NAME = "finviz_db"
ET = timezone(timedelta(hours=-4))          # matches the collectors' naive-ET
WATCHED = ["raw_ticks", "raw_quotes", "level2_snapshots"]


def _regular_hours(now_et: datetime) -> bool:
    if now_et.weekday() >= 5:
        return False
    return (9, 30) <= (now_et.hour, now_et.minute) < (16, 0)


def _counts(db) -> dict:
    out = {c: db[c].estimated_document_count() for c in WATCHED}
    out["candles(ibkr_tick)"] = db["candles"].count_documents({"source": "ibkr_tick"})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mongo", default=os.environ.get("MONGO_URI",
                                                      "mongodb://localhost:27017/"))
    ap.add_argument("--seconds", type=int, default=30,
                    help="gap between the two samples (default: 30)")
    args = ap.parse_args()

    client = MongoClient(args.mongo, serverSelectionTimeoutMS=5000)
    try:
        # Force the connection now. Without this the failure surfaces later, on
        # the first real query, as a page of pymongo internals — which says
        # "ServerSelectionTimeoutError" at the very bottom and nothing useful
        # anywhere a reader looks first.
        client.admin.command("ping")
    except PyMongoError as e:
        print(f"Cannot reach MongoDB at {args.mongo}\n  {type(e).__name__}\n")
        if ":27018" in args.mongo:
            print("27018 is the containers' MongoDB, so the usual cause is that")
            print("the stack is not up. Start Docker Desktop, then:")
            print("    cd deploy && docker compose up -d")
        else:
            print("Start your local mongod, or pass --mongo for the one you meant")
            print("(the containers publish theirs on mongodb://127.0.0.1:27018/).")
        raise SystemExit(1)

    db = client[DB_NAME]
    now_et = datetime.now(ET)
    rth = _regular_hours(now_et)

    print(f"mongo : {args.mongo}")
    print(f"time  : {now_et:%Y-%m-%d %H:%M} ET "
          f"({'regular hours' if rth else 'CLOSED — ticks are expected to be 0'})")

    first = _counts(db)
    print(f"\nsampling {args.seconds}s...")
    time.sleep(args.seconds)
    second = _counts(db)

    print()
    moving = []
    for name in first:
        delta = second[name] - first[name]
        mark = "RISING" if delta > 0 else "flat"
        if delta > 0:
            moving.append(name)
        print(f"  {name:<22} {second[name]:>9,}  {delta:+,} {mark}")

    print()
    if moving:
        print(f"LIVE: {', '.join(moving)} advanced. The tape is flowing.")
    elif not rth:
        print("Nothing moved, which is correct outside regular hours. This run")
        print("proves nothing either way — repeat it between 09:30 and 16:00 ET.")
    else:
        print("NOT LIVE: regular hours and nothing advanced. Check the collector")
        print("log for error 10189 or 162 — both mean another IBKR session holds")
        print("the market-data line, and both are worded as an IP-address problem.")


if __name__ == "__main__":
    main()
