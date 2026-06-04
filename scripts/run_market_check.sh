#!/usr/bin/env bash
#
# run_market_check.sh — headless wrapper around market_conditions.py for cron/launchd.
#
# Self-contained and cron-safe: absolute paths, minimal-PATH tolerant, never crashes the
# scheduler. Writes a dated, timestamped run log to data/logs/ in addition to the JSONL
# record the Python job appends itself. Read-only — touches no account and places no orders.
#
# Manual run:   scripts/run_market_check.sh
# Cron:         see scripts/README.md
#
set -euo pipefail

# Resolve repo root from this script's location (works regardless of cwd / cron PATH).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Absolute interpreter — cron's PATH usually won't find a framework python3.
PYTHON="${AGENTIC_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

LOG_DIR="${REPO}/data/logs"
mkdir -p "$LOG_DIR"
RUN_LOG="${LOG_DIR}/market_check_$(date +%Y-%m-%d).log"

STAMP="$(date '+%Y-%m-%dT%H:%M:%S%z')"
{
  echo "=== ${STAMP} run_market_check ==="
  # `|| true` so a transient data error logs but never fails the cron job.
  "$PYTHON" "${REPO}/scripts/market_conditions.py" 2>&1 || echo "market_conditions.py exited non-zero (logged above)"
  echo
} >> "$RUN_LOG" 2>&1

# Echo the latest summary to stdout too (useful when run by hand or by a Claude session).
tail -n 4 "$RUN_LOG"
