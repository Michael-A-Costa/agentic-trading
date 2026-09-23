#!/usr/bin/env bash
#
# run_trend.sh — one BTC-trend-sleeve check-in (IBIT on the agentic account), driven by launchd.
#
# Usage: run_trend.sh open|close|status
#   open   (~09:45 ET): reconcile, then act on yesterday's BTC close vs SMA50 (entry / exit / resize)
#   close  (~15:50 ET): reconcile + stop guard only
#   status (hourly, 24/7): READ-ONLY account check; macOS notification on any anomaly
#
# Places REAL orders only when TREND_ARMED=1 in .env; otherwise a dry-run (real review, nothing placed).
# Kill switches (in order of preference):
#   1. launchctl unload ~/Library/LaunchAgents/com.agentic.trend.plist  — stops the scheduler
#   2. TREND_ARMED=0 in .env                                            — dry-run
#   3. Disconnect the Robinhood MCP                                     — blocks all broker calls
# The resting GTC stop at Robinhood keeps protecting an open lot even with all three pulled.
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO" || exit 1

PHASE="${1:-}"
[[ "$PHASE" == "open" || "$PHASE" == "close" || "$PHASE" == "status" ]] || { echo "usage: $0 open|close|status" >&2; exit 2; }

set -a
[ -f "$REPO/.env" ] && . "$REPO/.env"
set +a
export PYTHONUNBUFFERED=1

# Weekends: nothing to do (IBIT doesn't trade). Holidays are caught in Python by quote freshness.
[ "$PHASE" != "status" ] && [ "$(date +%u)" -ge 6 ] && exit 0

PYTHON="${AGENTIC_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

LOG_DIR="${REPO}/data/logs"; mkdir -p "$LOG_DIR"
RUN_LOG="${LOG_DIR}/trend_$(date +%Y-%m-%d).log"
echo "[$(date '+%H:%M:%S')] trend ${PHASE} (TREND_ARMED=${TREND_ARMED:-0})" >> "$RUN_LOG"
"$PYTHON" "${REPO}/scripts/trend_sleeve.py" --phase "$PHASE" >> "$RUN_LOG" 2>&1
rc=$?
echo "[$(date '+%H:%M:%S')] trend ${PHASE} exit ${rc}" >> "$RUN_LOG"
exit $rc
