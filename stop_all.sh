#!/usr/bin/env bash
# stop_all.sh - stop the collector + the dashboard app.
# The current system runs only ibkr.dynamic_collector and `python -m app`; the
# other patterns below are older collector layouts (separate tick/level2
# processes, the supervisor) that are matched too so a machine left running an
# earlier version still gets cleaned up.
# (IB Gateway and MongoDB are left running - infrastructure, managed manually)
set -u
echo "== stopping =="

kill_pat () {
  local pat="$1" name="$2"
  local pids
  pids=$(pgrep -f "$pat" | tr '\n' ' ')
  if [ -n "${pids// /}" ]; then
    echo "  killing $name : $pids"
    pkill -f "$pat"
  else
    echo "  (none) $name"
  fi
}

# Supervisor first so it stops trying to restart children, then mop up anything
# started by hand (or leftover) directly.
kill_pat "collectors_supervisor"  "supervisor"
sleep 2
kill_pat "ibkr.tick_collector"    "tick_collector"
kill_pat "ibkr.dynamic_collector" "dynamic_collector"
kill_pat "ibkr.level2_collector"  "level2_collector"
kill_pat "ibkr.backfill"          "backfill (incl. stale)"
kill_pat "collect_9_tickers"      "collect_9_tickers (stale)"
kill_pat "python -m app"          "dash app (8050)"

sleep 2
echo "== remaining related processes =="
ps -eo pid,command | grep -iE "collectors_supervisor|ibkr\.|collect_9|python -m app" | grep -v grep || echo "  all stopped"
echo "== done =="
echo "Note: IB Gateway was NOT stopped."
