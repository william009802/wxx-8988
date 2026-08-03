#!/usr/bin/env bash
# macOS / Linux equivalent of windows-run.bat
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt
python3 selftest.py
export DB_PATH=./wxalpha.db START_BANKROLL=700 LIVE_TRADING=false
export USER_AGENT="wxalpha/1.0 change-me@example.com"
# Local runs live on the container filesystem by design, so the durability guard
# that protects a Railway volume has to be switched off here.
export REQUIRE_DURABLE_DB=false
# Reachability is reported, never worked around. There used to be a synthetic-data
# fallback on this branch, which meant one bad DNS lookup at boot could quietly
# turn a multi-week collection run into invented numbers.
if ! python3 check_apis.py --gamma-only; then
  echo "WARNING: Polymarket API unreachable from here. The bot will start and keep"
  echo "retrying, but it will find no markets until the network allows it."
fi
echo "dashboard http://localhost:8000"
exec python3 -m uvicorn app.main:app --port 8000
