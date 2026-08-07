#!/usr/bin/env bash
# start_all.sh — the whole system is just TWO processes now:
#   1) the UNIFIED collector  (python -m ibkr.dynamic_collector)
#        ticks -> 1sec bars + one-time 1sec catch-up backfill + L2 depth,
#        purely on-demand: nothing is collected until you search a ticker in the
#        app; the queue keeps the most-recently-searched (up to 5 tick lines /
#        3 depth lines) and evicts the oldest.
#   2) the DASH app           (python -m app, http://127.0.0.1:8050)
#
# No supervisor, no separate tick/dynamic/level2 collectors — one connection,
# one clientId (40; backfills use 41). The collector reconnects on its own.
# Prerequisite: IB Gateway running + logged in, API enabled on port 7497.
set -u
cd "$(dirname "$0")"

# Interpreter resolution, most specific first, so the repo runs on any machine:
#   1. $PYTHON            — explicit override, e.g.  PYTHON=python3.12 ./start_all.sh
#   2. an activated venv  — $VIRTUAL_ENV/bin/python
#   3. ./venv_main        — the venv this repo creates by convention
#   4. python3 on PATH
if [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
    PY="$VIRTUAL_ENV/bin/python"
elif [ -x "venv_main/bin/python" ]; then
    PY="venv_main/bin/python"
else
    PY="$(command -v python3 || true)"
fi

if [ -z "$PY" ] || ! "$PY" -c "import dash" 2>/dev/null; then
    echo "ERROR: no usable Python found (tried: ${PY:-none})."
    echo "  Create the venv and install dependencies first:"
    echo "    python3 -m venv venv_main"
    echo "    ./venv_main/bin/pip install -r requirements.txt"
    echo "  Or point PYTHON at an interpreter that already has them:"
    echo "    PYTHON=/path/to/python ./start_all.sh"
    exit 1
fi
echo "  using interpreter: $PY"

echo "== starting =="

# 1) Unified collector. caffeinate keeps the Mac awake for the long run.
#    Add --no-l2 to skip depth; --pin with no names for a purely on-demand set.
echo "  -> unified collector (on-demand queue, up to 5 tickers, L2 depth)"
caffeinate -i nohup "$PY" -m ibkr.dynamic_collector >> logs_collector.log 2>&1 &

# 2) Dashboard (port 8050)
echo "  -> dash app (http://127.0.0.1:8050)"
nohup "$PY" -m app >> logs_app.log 2>&1 &

sleep 5
echo "== status after start =="
ps -eo pid,command | grep -iE "ibkr\.dynamic_collector|python -m app" | grep -v grep
echo ""
echo "Logs:"
echo "  tail -f logs_collector.log   # ticks + backfill + L2 depth (everything)"
echo "  tail -f logs_app.log         # dashboard"
