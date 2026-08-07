#!/bin/bash
# scripts/run_grid_daily.sh
# Regenerates the CVD<->price session/timeframe grid on all clean-feed days
# (auto-detected from 2026-07-21 onward). Safe to run any time — reads MongoDB
# only. Wired to a launchd agent for a daily run; also fine to run by hand.
#
#   Enable  : launchctl load  ~/Library/LaunchAgents/com.tradingcvd.grid.plist
#   Disable : launchctl unload ~/Library/LaunchAgents/com.tradingcvd.grid.plist

set -euo pipefail
# Resolve the repo from this script's own location (scripts/ -> repo root) so the
# launchd agent works regardless of where the repo is checked out.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# $PYTHON overrides; otherwise prefer the repo venv, then python3 on PATH.
if [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
elif [ -x "$REPO/venv_main/bin/python" ]; then
    PY="$REPO/venv_main/bin/python"
else
    PY="$(command -v python3)"
fi
LOG="$REPO/scripts/grid_daily.log"

cd "$REPO"
echo "===== $(date '+%Y-%m-%d %H:%M:%S') running session_tf_grid =====" >> "$LOG"
if ! "$PY" scripts/session_tf_grid.py >> "$LOG" 2>&1; then
    echo "[grid] FAILED (is MongoDB running?)" >> "$LOG"
    exit 1
fi
# Keep a dated snapshot alongside the always-current report.
cp "$REPO/scripts/CVD_session_tf_grid_report.md" \
   "$REPO/scripts/CVD_session_tf_grid_$(date '+%Y%m%d').md"
echo "[grid] done" >> "$LOG"
