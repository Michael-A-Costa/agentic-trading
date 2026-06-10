#!/usr/bin/env bash
#
# run_sentinel.sh — one FAST sentinel pass (PAPER mode), driven by launchd ~every minute.
#
# The fast half of the two-rate loop (see docs/two-rate-architecture.md): deterministic protective
# exits + armed-entry triggers, NO LLM. Shares data/.tick.lock with the planner (run_trading_tick.sh)
# so the two never race on paper_state.json — a sentinel pass skips while a planner tick runs.
# Sources .env so the sentinel's exit rules/caps are IDENTICAL to the planner's.
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO" || exit 1

set -a
[ -f "$REPO/.env" ] && . "$REPO/.env" || { [ -f "$REPO/.env.example" ] && . "$REPO/.env.example"; }
set +a

PYTHON="${AGENTIC_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

# LIVE: run the live fast pass — checks FRACTIONAL/synthetic stops against fresh public quotes every
# minute and fires a protective sell on a breach (whole-share lots are covered by resting broker stops).
# PAPER: the original sentinel manages the simulated book.
if [ "${TRADING_MODE:-paper}" = "live" ]; then
  exec "$PYTHON" "${REPO}/scripts/live_sentinel.py" "$@"
fi

exec "$PYTHON" "${REPO}/scripts/sentinel.py" "$@"
