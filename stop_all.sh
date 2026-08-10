#!/usr/bin/env bash
# Thin wrapper around stop_all.py (see start_all.sh for why). Any arguments are
# passed through, so `./stop_all.sh --scan` still does the old pattern-matching
# sweep for processes that were not started by start_all.py.
#
# IB Gateway and MongoDB are left running - infrastructure, managed manually.
set -u
cd "$(dirname "$0")"

if [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
    PY="$VIRTUAL_ENV/bin/python"
elif [ -x "venv_main/bin/python" ]; then
    PY="venv_main/bin/python"
else
    PY="$(command -v python3 || true)"
fi

if [ -z "$PY" ]; then
    echo "ERROR: no python3 found."
    exit 1
fi

exec "$PY" stop_all.py "$@"
