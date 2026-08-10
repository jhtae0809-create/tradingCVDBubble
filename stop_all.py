#!/usr/bin/env python3
"""Stop the collector + the dashboard app. Cross-platform counterpart to
start_all.py (see there for why these are Python rather than shell scripts).

    python stop_all.py

Processes started by start_all.py are recorded in .run_pids.json and stopped by
pid. Anything started by hand (or by an older revision of this project) is not
in that file, so `--scan` is offered as a fallback that matches on the command
line instead — on POSIX only, since it needs pgrep/pkill.

IB Gateway and MongoDB are left running: they are infrastructure, managed
separately from this project.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
PIDFILE = REPO / ".run_pids.json"
IS_WINDOWS = os.name == "nt"

# Older layouts (separate tick/level2 collectors, the supervisor) are matched too
# so a machine left running an earlier version still gets cleaned up.
SCAN_PATTERNS = [
    ("collectors_supervisor", "supervisor"),
    ("ibkr.dynamic_collector", "dynamic_collector"),
    ("ibkr.tick_collector", "tick_collector"),
    ("ibkr.level2_collector", "level2_collector"),
    ("ibkr.backfill", "backfill (incl. stale)"),
    ("python -m app", "dash app (8050)"),
]


# Shared with the launcher rather than copied: the Windows/zombie handling in
# process_alive is subtle enough that two drifting copies would be a liability.
from start_all import process_alive  # noqa: E402


def terminate(pid: int, force: bool = False) -> None:
    if IS_WINDOWS:
        # taskkill /F is already forceful; /T also stops children (the collector
        # spawns backfill subprocesses).
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass


def stop_by_pidfile() -> bool:
    if not PIDFILE.exists():
        print("  (no .run_pids.json — nothing recorded as started by start_all.py)")
        return False
    try:
        pids = json.loads(PIDFILE.read_text())
    except Exception as e:
        print(f"  could not read {PIDFILE.name}: {e}")
        return False

    stopped_any = False
    for name, pid in pids.items():
        if not isinstance(pid, int):
            continue
        if not process_alive(pid):
            print(f"  (already stopped) {name} : pid {pid}")
            continue
        print(f"  stopping {name} : pid {pid}")
        terminate(pid)
        stopped_any = True

    # Give SIGTERM a moment to run the shutdown paths (the collector cancels its
    # subscriptions and disconnects cleanly), then escalate on anything that
    # ignored it, so "stopped" is never reported for a process still holding
    # clientId 40 or port 8050.
    if stopped_any:
        time.sleep(2)
        stubborn = [(n, p) for n, p in pids.items()
                    if isinstance(p, int) and process_alive(p)]
        for name, pid in stubborn:
            print(f"  {name} (pid {pid}) ignored SIGTERM — forcing")
            terminate(pid, force=True)
        if stubborn:
            time.sleep(1)
            for name, pid in stubborn:
                if process_alive(pid):
                    print(f"  WARNING: {name} (pid {pid}) is STILL running — "
                          f"stop it manually")

    PIDFILE.unlink(missing_ok=True)
    return stopped_any


def stop_by_scan() -> None:
    if IS_WINDOWS:
        sys.exit("ERROR: --scan needs pgrep/pkill and is POSIX-only.\n"
                 "  On Windows, stop the two python.exe processes from Task\n"
                 "  Manager, or delete .run_pids.json and re-run start_all.py.")
    for pattern, label in SCAN_PATTERNS:
        found = subprocess.run(["pgrep", "-f", pattern],
                               capture_output=True, text=True).stdout.split()
        if found:
            print(f"  killing {label} : {' '.join(found)}")
            subprocess.run(["pkill", "-f", pattern], capture_output=True)
        else:
            print(f"  (none) {label}")
        if label == "supervisor" and found:
            time.sleep(2)   # let it stop restarting children first


def main() -> None:
    ap = argparse.ArgumentParser(description="Stop the collector and the dashboard.")
    ap.add_argument("--scan", action="store_true",
                    help="also match by command line, for processes not started "
                         "by start_all.py (POSIX only)")
    args = ap.parse_args()

    print("== stopping ==")
    stop_by_pidfile()
    if args.scan:
        stop_by_scan()
    print("== done ==")
    print("Note: IB Gateway and MongoDB were NOT stopped.")


if __name__ == "__main__":
    main()
