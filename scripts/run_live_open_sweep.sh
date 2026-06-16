#!/usr/bin/env bash
#
# run_live_open_sweep.sh — market-open DD burst (LIVE mode).
#
# Fires the morning DD slate ALL AT ONCE instead of ~5/tick: refreshes regime + the LIVE context
# (broker snapshot -> live_tick_context), then runs open_dd_sweep.py, which drains the full candidate
# list through a bounded pool of background dd_worker.py processes. Verdicts land in data/dd_jobs/;
# a finished COMMIT force-triggers a normal live tick that ingests + acts on it (see dd_worker.py),
# and the next scheduled tick mops up the rest. DISPATCH-ONLY — this wrapper places no orders and
# writes no DD cache (decide.py stays the sole writer); all real review/place still flows through the
# normal LIVE tick with its full caps + arming gate.
#
# Self-gating: open_dd_sweep.py runs at most once per ET trading day, only inside the open window,
# only when context allows entries — so this is safe to schedule dumbly on StartInterval (it no-ops
# outside the window / after it has already swept). Pairs with com.agentic.open-sweep-live.plist.
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO" || exit 1

# Load config, then FORCE live mode regardless of TRADING_MODE in .env (mode = which script runs).
# TRADING_MODE=live makes the dd_worker force-trigger fire the LIVE tick (run_live_tick.sh).
set -a
[ -f "$REPO/.env" ] && . "$REPO/.env" || { [ -f "$REPO/.env.example" ] && . "$REPO/.env.example"; }
TRADING_MODE=live
set +a

export PYTHONUNBUFFERED=1
PYTHON="${AGENTIC_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

LOG_DIR="${REPO}/data/logs"; mkdir -p "$LOG_DIR"
RUN_LOG="${LOG_DIR}/live-open-sweep_$(date +%Y-%m-%d).log"
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
# data pulls (regime) or broker snapshots all day. --gate-only returns 10 to skip.
"$PYTHON" "${REPO}/scripts/open_dd_sweep.py" --gate-only || { log "out of open window — skip"; exit 0; }

{
  SECONDS=0
  # In the window: refresh regime + the LIVE context so allow_entries / candidates / held reflect the
  # open, exactly like a live tick (market regime -> broker snapshot -> live_tick_context). The sweep
  # reads the context_latest.json this writes. broker_snapshot is fail-soft here: if it can't fetch,
  # open_dd_sweep falls back to the prior context (and worst case just no-ops on a stale slate).
  "$PYTHON" "${REPO}/scripts/market_conditions.py" --quiet || log "market_conditions failed (continuing)"
  "$PYTHON" "${REPO}/scripts/broker_snapshot.py" >/dev/null 2>>"$RUN_LOG" || log "broker_snapshot failed (continuing on prior context)"
  "$PYTHON" "${REPO}/scripts/live_tick_context.py" >/dev/null 2>>"$RUN_LOG" || log "live_tick_context failed (continuing)"
  "$PYTHON" "${REPO}/scripts/open_dd_sweep.py" | tee -a "$RUN_LOG"
  log "=== live open-sweep wrapper end (${SECONDS}s) ==="
} 2>&1 | tee -a "$RUN_LOG"
