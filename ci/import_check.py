#!/usr/bin/env python3
"""Import every module on the live path and log a line containing the same
non-ASCII characters the collector emits.

Run by .github/workflows/windows-smoke.yml under codepage 1252 with stdout
redirected to a file — the configuration in which Windows picks the ANSI
codepage for stdout and the project's arrows/box characters used to raise
UnicodeEncodeError. Deliberately does NOT set PYTHONUTF8 itself: the point is to
prove the launcher's environment (or the code's own choices) make this safe.

Usable on any platform:  python ci/import_check.py
"""

import importlib
import logging
import sys
from pathlib import Path

# sys.path[0] is ci/, not the repo root, so the project's packages would not be
# importable otherwise.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MODULES = [
    "app",
    "ibkr.dynamic_collector",
    "ibkr.backfill",
    "finviz.new_finviz",
    "finviz.errors",
    "history.rollup",
    "history.store",
    "cvd.calculator",
    "cvd.visualizer",
    "cvd.visualizer_level2",
    "start_all",
    "stop_all",
]

# The exact characters found in this project's print/logging calls.
SAMPLE = "— → ─ ⚠"


def main() -> int:
    print(f"stdout encoding: {sys.stdout.encoding}")
    failed = []
    for name in MODULES:
        try:
            importlib.import_module(name)
            print(f"  ok   {name}")
        except Exception as e:
            failed.append((name, e))
            print(f"  FAIL {name}: {e!r}")

    # Fails loudly here rather than three days into a collection run.
    try:
        print(f"non-ASCII print: {SAMPLE}")
        logging.basicConfig(stream=sys.stdout, level=logging.INFO, force=True)
        logging.info("non-ASCII log: %s", SAMPLE)
    except UnicodeEncodeError as e:
        print(f"FAIL: console encoding cannot carry this project's output: {e}")
        return 1

    if failed:
        print(f"\n{len(failed)} module(s) failed to import")
        return 1
    print("\nall modules imported")
    return 0


if __name__ == "__main__":
    sys.exit(main())
