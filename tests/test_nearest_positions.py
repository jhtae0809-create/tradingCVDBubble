"""Regression test for cvd.visualizer.nearest_positions.

The anchor jump ("Jump to (ET)") locates the pinned bar with this function and
passes its target as a plain list of one Timestamp. numpy turns that into an
OBJECT array, and `datetime64 - object` raises rather than coercing — so the
centering silently fell back to the tail of the loaded window, which looks
like a working chart whenever the tail happens to hold data, and like a blank
chart when it does not.

Run: python -m tests.test_nearest_positions
"""

import numpy as np
import pandas as pd

from cvd.visualizer import nearest_positions

IDX = pd.date_range("2026-07-22 09:30", periods=10, freq="1min")

CASES = [
    # (name, index, targets, expected)
    ("list of Timestamp",  IDX, [pd.Timestamp("2026-07-22 09:34:20")], [4]),
    ("DatetimeIndex",      IDX, pd.DatetimeIndex(["2026-07-22 09:34:20"]), [4]),
    ("microsecond index",  IDX.astype("datetime64[us]"),
                                [pd.Timestamp("2026-07-22 09:34:20")], [4]),
    ("exact hit",          IDX, [pd.Timestamp("2026-07-22 09:33")], [3]),
    ("before the start",   IDX, [pd.Timestamp("2026-07-01")], [0]),
    ("after the end",      IDX, [pd.Timestamp("2026-08-01")], [9]),
    # The raw_tick frame repeats timestamps; get_indexer refuses such an index
    # outright, which is why this function exists.
    ("duplicate stamps",   pd.DatetimeIndex(list(IDX[:3]) * 2).sort_values(),
                                [pd.Timestamp("2026-07-22 09:31:10")], [3]),
    ("numeric index",      np.array([0.0, 1.0, 2.0, 3.0]), [2.6], [3]),
    ("empty targets",      IDX, [], []),
    ("empty index",        pd.DatetimeIndex([]), [pd.Timestamp("2026-07-22")], []),
]


def main():
    failed = 0
    for name, index, targets, expected in CASES:
        got = list(nearest_positions(index, targets))
        ok = got == list(expected)
        failed += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<20} -> {got} (expected {list(expected)})")
    print("FAILED" if failed else "all cases pass")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
