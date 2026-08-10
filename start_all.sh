#!/usr/bin/env bash
# Thin wrapper around start_all.py, which is the real launcher and works on
# macOS, Linux AND Windows. All the logic (dependency / MongoDB preflight,
# finviz/api_keys.py bootstrap, process spawning, pidfile) lives there so there
# is exactly one implementation to keep correct; this file exists so that
# ./start_all.sh keeps working for anyone (or any doc) that already uses it.
#
# What remains here is interpreter resolution, which is genuinely shell-side:
# start_all.py must be RUN BY the interpreter that has the dependencies.
#   1. $PYTHON            — explicit override, e.g.  PYTHON=python3.12 ./start_all.sh
#   2. an activated venv  — $VIRTUAL_ENV/bin/python
#   3. ./venv_main        — the venv this repo creates by convention
#   4. python3 on PATH
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
    echo "  Create the venv and install dependencies first:"
    echo "    python3 -m venv venv_main"
    echo "    ./venv_main/bin/pip install -r requirements.txt"
    echo "  Or point PYTHON at an interpreter that already has them:"
    echo "    PYTHON=/path/to/python ./start_all.sh"
    exit 1
fi

exec "$PY" start_all.py "$@"
