#!/usr/bin/env bash
#
# run_paper_open_sweep.sh — market-open DD burst (PAPER mode).
#
# Fires the morning DD slate ALL AT ONCE instead of ~5/tick: refreshes regime + context, then runs
# open_dd_sweep.py, which drains the full candidate list through a bounded pool of background
# dd_worker.py processes. Verdicts land in data/dd_jobs/ and the next normal paper tick ingests +
# acts on them. Dispatch-only — places no orders, writes no DD cache (decide.py stays sole writer).
#
# Self-gating: open_dd_sweep.py runs at most once per ET trading day, only inside the open window,
# only when context allows entries — so this is safe to schedule dumbly on StartInterval (it no-ops
# outside the window / after it has already swept). Pairs with com.agentic.open-sweep-paper.plist.
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO" || exit 1

# Load config, then FORCE paper mode regardless of TRADING_MODE in .env (mode = which script runs).
set -a
[ -f "$REPO/.env" ] && . "$REPO/.env" || { [ -f "$REPO/.env.example" ] && . "$REPO/.env.example"; }
TRADING_MODE=paper
set +a

export PYTHONUNBUFFERED=1
PYTHON="${AGENTIC_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

LOG_DIR="${REPO}/data/logs"; mkdir -p "$LOG_DIR"
RUN_LOG="${LOG_DIR}/paper-open-sweep_$(date +%Y-%m-%d).log"
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$RUN_LOG"; }

# Dedicated lock (NOT the tick lock — the sweep must run alongside ticks). The dispatch loop can run
# up to OPEN_SWEEP_DEADLINE_S (~12 min), so treat a >20-min-old lock as stale.
LOCK="${REPO}/data/.opensweep.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +20 2>/dev/null)" ]; then
    log "stale lock — reclaiming"; rmdir "$LOCK" 2>/dev/null; mkdir "$LOCK" 2>/dev/null || exit 0
  else
    log "another open-sweep is running — skip"; exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# Cheap pre-gate (no network): if we're outside the ET open window or already swept today, exit
# WITHOUT refreshing regime/context — so scheduling this dumbly on StartInterval doesn't double the
# Cboe data pulls all day (which would risk rate-limiting the real tick). --gate-only returns 10 to skip.
"$PYTHON" "${REPO}/scripts/open_dd_sweep.py" --gate-only || { log "out of open window — skip"; exit 0; }

{
  SECONDS=0
  # In the window: refresh regime + context so allow_entries / candidates reflect the open, like a tick.
  "$PYTHON" "${REPO}/scripts/market_conditions.py" --quiet || log "market_conditions failed (continuing)"
  "$PYTHON" "${REPO}/scripts/tick_context.py" >/dev/null || log "tick_context failed (continuing)"
  "$PYTHON" "${REPO}/scripts/open_dd_sweep.py" | tee -a "$RUN_LOG"
  log "=== open-sweep wrapper end (${SECONDS}s) ==="
} 2>&1 | tee -a "$RUN_LOG"
