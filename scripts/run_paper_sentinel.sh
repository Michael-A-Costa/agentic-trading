#!/usr/bin/env bash
#
# run_paper_sentinel.sh — one PAPER fast sentinel pass, driven by launchd ~every minute.
#
# PAPER mode only. Deterministic protective exits + armed-entry triggers, NO LLM.
# Shares data/.tick.lock with run_paper_tick.sh — a sentinel pass skips if the planner
# is running (never race on paper_state.json).
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO" || exit 1

set -a
[ -f "$REPO/.env" ] && . "$REPO/.env" || { [ -f "$REPO/.env.example" ] && . "$REPO/.env.example"; }
TRADING_MODE=paper
set +a

PYTHON="${AGENTIC_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

exec "$PYTHON" "${REPO}/scripts/sentinel.py" "$@"
