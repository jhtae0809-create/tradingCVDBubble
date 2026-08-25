#!/usr/bin/env python3
"""Container entrypoint: wait for MongoDB, seed the demo day, serve the app.

Split out of the Dockerfile CMD so the startup logic is readable, testable and
identical whether it runs on Railway, on plain Docker or by hand.

Three things have to happen in order, and each one has a failure mode that is
invisible if it is skipped:

  1. WAIT for MongoDB. Railway starts services in parallel, so on a cold deploy
     the database is routinely not accepting connections yet. Without a wait the
     app comes up, every chart request stalls on server selection, and the logs
     show nothing but timeouts.

  2. SEED demo_data/ if the candles collection is empty. A fresh Railway volume
     is an empty database, and the dashboard against an empty database is a
     working page with no chart on it — the single most common way this project
     looks broken when it is not. Seeding is skipped when data is already there,
     so a restart costs nothing and live-collected data is never overwritten.

  3. EXEC gunicorn. exec, not subprocess: gunicorn replaces this process and so
     becomes PID 1, which is what receives SIGTERM when the platform stops the
     container. Wrapped in a parent, signals go to the wrapper and the platform
     ends up killing the workers instead of letting them drain.
"""

import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
SEED = os.environ.get("DEMO_SEED", "1") != "0"
WAIT_SECONDS = int(os.environ.get("MONGO_WAIT_SECONDS", "90"))


def wait_for_mongo():
    from pymongo import MongoClient
    deadline = time.time() + WAIT_SECONDS
    attempt = 0
    while True:
        attempt += 1
        try:
            client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
            client.admin.command("ping")
            print(f"[start] MongoDB reachable (attempt {attempt})", flush=True)
            return client
        except Exception as e:
            if time.time() >= deadline:
                sys.exit(f"[start] FATAL: MongoDB unreachable after "
                         f"{WAIT_SECONDS}s: {e.__class__.__name__}: {e}\n"
                         f"[start] MONGO_URI={MONGO_URI}\n"
                         "[start] On Railway, add a MongoDB service and set this\n"
                         "[start] app's MONGO_URI to its connection string.")
            print(f"[start] waiting for MongoDB ({e.__class__.__name__})...",
                  flush=True)
            time.sleep(3)


def seed_if_empty(client):
    if not SEED:
        print("[start] DEMO_SEED=0 — skipping the demo dataset", flush=True)
        return
    n = client["finviz_db"]["candles"].estimated_document_count()
    if n:
        print(f"[start] candles collection already holds ~{n:,} docs "
              "— not seeding", flush=True)
        return
    print("[start] empty database — loading demo_data/ ...", flush=True)
    from scripts import demo_dataset
    demo_dataset.load()
    print("[start] demo dataset loaded", flush=True)


def main():
    client = wait_for_mongo()
    try:
        seed_if_empty(client)
    except Exception as e:
        # A failed seed must not stop the app from starting: the dashboard is
        # still usable against whatever is in the database, and an empty chart
        # with a readable log beats a container that will not boot at all.
        print(f"[start] WARNING: demo seed failed ({e.__class__.__name__}: {e})"
              " — starting the app anyway", flush=True)

    port = os.environ.get("PORT", "8050")
    argv = [
        "gunicorn", "app:server",
        "--bind", f"0.0.0.0:{port}",
        # ONE worker on purpose. app.py keeps per-process state (the FinViz
        # error banner, the fetch-throttle timestamps, the tiered-serve cache),
        # so a second worker would answer half the requests from a different,
        # colder copy of it. Threads give concurrency without splitting state.
        "--workers", "1",
        "--threads", "4",
        # Chart requests do real work (tier rollups, L2 grid builds) and can run
        # past gunicorn's 30s default, which would kill the worker mid-render.
        "--timeout", "120",
        "--access-logfile", "-",
        "--error-logfile", "-",
    ]
    print(f"[start] exec: {' '.join(argv)}", flush=True)
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
