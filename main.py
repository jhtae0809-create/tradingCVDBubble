"""
main.py
-------
Full pipeline:
  1. Initialization      (finviz_curl.py)
  2. Fetch + save        (finviz/new_finviz.py  → MongoDB)
  3. CVD calculation     (cvd/calculator.py)
  4. Visualization       (cvd/visualizer.py)
  5. Loop every 60s      (fetch → recalculate → refresh chart)

Usage:
  python main.py               # default: NVDA, loop on
  python main.py --ticker AAPL
  python main.py --ticker TSLA --no-loop   # one-shot, no repeat
"""

import os
import argparse
import random
import time
import traceback

from finviz.finviz_curl import login, get_token, update_api_keys
from finviz.new_finviz import fetch_and_save, FinvizTokenError
from cvd.calculator import run_pipeline
from cvd.visualizer import build_chart, write_chart_html
import cvd.visualizer_level2 as visualizer_level2
from TradingView.admin import DOWNLOAD_DIR


# ─────────────────────────────────────────
# 1. Initializing System Variables
# ─────────────────────────────────────────

# Refreshes the Finviz token
def refresh_token():
    print("\n[Token] Regenerating FinViz API token...")
    try:
        session = login()
        token   = get_token(session)
        update_api_keys(token)
        print(f"[Token] ✅ Token updated.")
    except SystemExit:
        print("[Token] ❌ Token regeneration failed — continuing with existing token.")

# Creates "data" directory (if needed)
def create_data_dir():
    try:
        os.mkdir(DOWNLOAD_DIR)
        print(f"Directory '{DOWNLOAD_DIR}' created successfully.")
    except FileExistsError:
        print(f"Directory '{DOWNLOAD_DIR}' already exists.")
    except PermissionError:
        print(f"Permission denied: Unable to create '{DOWNLOAD_DIR}'.")
    except Exception as e:
        print(f"An error occurred: {e}")

# ─────────────────────────────────────────
# 2. Fetch + save to MongoDB
# ─────────────────────────────────────────

def fetch(ticker: str):
    print(f"\n[Fetch] Fetching 1-min candles for {ticker}...")
    candles = fetch_and_save(ticker, timeframe="i1")
    print(f"[Fetch] ✅ {len(candles)} candles fetched and saved.")
    return candles


# ─────────────────────────────────────────
# 3 + 4. Calculate + visualize
# ─────────────────────────────────────────

def calculate_and_show(ticker: str, save_html: bool = True, open_browser: bool = False, use_level2: bool = False):
    print(f"\n[Pipeline] Running CVD pipeline for {ticker} (Level 2: {use_level2})...")
    df_1min, frames = run_pipeline(ticker)

    if df_1min.empty or not frames:
        print("[Pipeline] ❌ No data to visualize.")
        return

    if use_level2:
        fig = visualizer_level2.build_chart(df_1min, frames, ticker)
        path = "chart_level2.html"
    else:
        fig = build_chart(df_1min, frames, ticker)
        path = "chart.html"

    if save_html:
        if use_level2:
            visualizer_level2.write_chart_html(fig, path)
        else:
            write_chart_html(fig, path)
        print(f"[Visualizer] Saved → {path}")

    if open_browser:
        import webbrowser
        from pathlib import Path
        # as_uri(), not an f-string: on Windows the absolute path is
        # C:\\Users\\..., and "file://" + that keeps the backslashes and is one
        # slash short, so the browser gets a malformed URL and opens nothing.
        webbrowser.open(Path(path).resolve().as_uri())


# ─────────────────────────────────────────
# 5. Main loop
# ─────────────────────────────────────────

def run(ticker: str, loop: bool = True, interval: int = 60, use_level2: bool = False):
    print(f"\n{'='*55}")
    print(f"  CVD Pipeline  |  ticker={ticker}  |  loop={loop} | level2={use_level2}")
    print(f"{'='*55}")

    # Token regeneration once at startup
    refresh_token()

    # Schedule next proactive token refresh:
    # ~12h worth of iterations (720 @ 60s) ± random jitter to avoid a fixed pattern.
    def next_refresh_iter(current: int) -> int:
        base = (12 * 60 * 60) // max(interval, 1)   # iterations in ~12h
        jitter = random.randint(-10, 10)
        return current + base + jitter

    refresh_at = next_refresh_iter(0)

    iteration = 0
    while True:
        iteration += 1
        print(f"\n[Loop] ── Iteration {iteration} ──────────────────────")

        # Proactive token refresh before it expires (~24h lifetime)
        if iteration >= refresh_at:
            print(f"[Loop] Scheduled token refresh (iter {iteration}).")
            refresh_token()
            refresh_at = next_refresh_iter(iteration)

        try:
            fetch(ticker)
            calculate_and_show(ticker, save_html=True, open_browser=(iteration == 1), use_level2=use_level2)
        except FinvizTokenError as e:
            # Wrong/expired token → regenerate and retry this iteration once.
            print(f"[Loop] ⚠️  Token error: {e}")
            print("[Loop] Regenerating token and retrying...")
            refresh_token()
            try:
                fetch(ticker)
                calculate_and_show(ticker, save_html=True, open_browser=(iteration == 1), use_level2=use_level2)
            except Exception:
                print("[Loop] ❌ Retry after token refresh still failed:")
                traceback.print_exc()
        except Exception:
            print("[Loop] ❌ Error during pipeline:")
            traceback.print_exc()

        if not loop:
            print("\n[Loop] One-shot mode — exiting.")
            break

        print(f"\n[Loop] Sleeping {interval}s until next fetch...")
        time.sleep(interval)


# ─────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────

if __name__ == "__main__":
    create_data_dir()
    parser = argparse.ArgumentParser(description="CVD Pipeline")
    parser.add_argument("--ticker",   type=str, default="NVDA", help="Stock ticker (default: NVDA)")
    parser.add_argument("--no-loop",  action="store_true",       help="Run once and exit")
    parser.add_argument("--interval", type=int, default=60,      help="Fetch interval in seconds (default: 60)")
    parser.add_argument("--level2",   action="store_true",       help="Use the new Level 2 visualization")
    args = parser.parse_args()

    run(
        ticker   = args.ticker,
        loop     = not args.no_loop,
        interval = args.interval,
        use_level2 = args.level2
    )
