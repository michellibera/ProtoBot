#!/usr/bin/env bash
cd "$(dirname "$(readlink -f "$0")")" || exit 1

[ -d venv ] || python3 -m venv venv
# Every start, so new dependencies reach an existing venv (fast when already installed).
venv/bin/pip install -q -r requirements.txt

trap 'echo; echo "stopped"; exit 0' INT TERM

while true; do
    venv/bin/python dispatch.py
    echo "[$(date '+%F %T')] dispatcher exited, restarting in 5s (Ctrl+C = stop)"
    sleep 5
done
