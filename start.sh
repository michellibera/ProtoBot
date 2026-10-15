#!/usr/bin/env bash
cd "$(dirname "$(readlink -f "$0")")" || exit 1

if [ ! -d venv ]; then
    python3 -m venv venv
    venv/bin/pip install -r requirements.txt
fi

trap 'echo; echo "stopped"; exit 0' INT TERM

while true; do
    venv/bin/python dispatch.py
    echo "[$(date '+%F %T')] dispatcher exited, restarting in 5s (Ctrl+C = stop)"
    sleep 5
done
