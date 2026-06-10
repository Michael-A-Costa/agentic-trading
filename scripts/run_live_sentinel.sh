#!/usr/bin/env bash
#
# run_live_sentinel.sh — one LIVE fast sentinel pass, driven by launchd ~every minute.
#
# LIVE mode only. Checks FRACTIONAL/synthetic stops against fresh public quotes every
# minute and fires a protective sell on a breach (whole-share lots are covered by resting
# broker stops). NO LLM.
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO" || exit 1

set -a
[ -f "$REPO/.env" ] && . "$REPO/.env" || { [ -f "$REPO/.env.example" ] && . "$REPO/.env.example"; }
TRADING_MODE=live
set +a

PYTHON="${AGENTIC_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

exec "$PYTHON" "${REPO}/scripts/live_sentinel.py" "$@"
