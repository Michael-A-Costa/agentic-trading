#!/usr/bin/env bash
#
# run_live_tick.sh — one LIVE trading tick, driven by launchd/cron.
#
# LIVE mode only. This script places REAL orders against account your_account_number.
#
# Two-step arming gate (both must be set to place orders):
#   TRADING_MODE=live   — set by this script unconditionally
#   LIVE_ARMED=1        — must be set in .env; without it this is a DRY-RUN (review only)
#
# Kill switches (in order of preference):
#   1. launchctl unload com.agentic.trading-live.plist  — stops the scheduler
#   2. LIVE_ARMED=0 in .env                             — arms nothing new (dry-run)
#   3. Disconnect the Robinhood MCP                     — blocks all relay calls
#
# Flow: precheck -> lock -> market regime -> broker_snapshot -> context+gate
#       (live_tick_context.py) -> [LLM decision] -> live_execute+log
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO" || exit 1

# Load config (.env preferred, else .env.example defaults), then FORCE live mode regardless
# of whatever TRADING_MODE is set to in .env — the mode is determined by which script you run.
set -a
[ -f "$REPO/.env" ] && . "$REPO/.env" || { [ -f "$REPO/.env.example" ] && . "$REPO/.env.example"; }
TRADING_MODE=live
set +a

# Stream child python stdout/stderr line-by-line (no block buffering through the tee pipe), so the
# per-call progress narration appears live in the terminal + run log instead of an end-of-tick burst.
export PYTHONUNBUFFERED=1

PYTHON="${AGENTIC_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
DD_MODEL="${DD_MODEL:-claude-sonnet-4-6}"

LOG_DIR="${REPO}/data/logs"; mkdir -p "$LOG_DIR"
RUN_LOG="${LOG_DIR}/live-tick_$(date +%Y-%m-%d).log"
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$RUN_LOG"; }

EXEC="${REPO}/scripts/live_execute.py"

if [ "${LIVE_ARMED:-0}" = "1" ]; then
  log "WARNING: LIVE_ARMED=1 — REAL orders will be placed (caps are the gate)."
else
  log "live DRY-RUN (LIVE_ARMED!=1): will review + log intended orders, place nothing."
fi

# 0) Fast hours pre-check (pure time math, no I/O) — bail before the 2-3 min broker snapshot
#    when the market is plainly closed.
PRECHECK="$("$PYTHON" "${REPO}/scripts/live_tick_context.py" --precheck 2>>"$RUN_LOG")"
if [[ "$PRECHECK" == GATE=SKIP:market_* ]]; then
  log "gate: ${PRECHECK} (precheck — skipping broker snapshot)"
  log "=== tick end (skipped) ==="
  exit 0
fi

# --- single-flight lock (atomic mkdir + PID-tagged owner) ---
# The 10-sec sentinel shares this lock but holds it only a few seconds, and only when it actually
# fires an exit/trim/ratchet. So DON'T skip the whole 4-min tick on contention — read the holder's
# PID from "$LOCK/owner" and decide:
#   • holder PID is DEAD (crash/SIGKILL left the dir behind) -> reclaim NOW (seconds, not 15 min)
#   • holder is ALIVE (sentinel mid-pass, or another planner) -> wait it out; the sentinel frees
#     within seconds, a concurrent planner never does -> skip after LOCK_WAIT_S to avoid overlap
# The >15-min find is now only a backstop for the rare PID-reuse case (dead PID reused, looks alive);
# it stays well above any legit planner hold (~3-4 min) so it can't reclaim a live slow tick.
LOCK="${REPO}/data/.tick.lock"
LOCK_WAIT_S="${TICK_LOCK_WAIT_S:-30}"
lock_owner_pid() { awk '{print $1; exit}' "$LOCK/owner" 2>/dev/null; }
lock_holder_dead() {
  local pid; pid="$(lock_owner_pid)"
  [ -z "$pid" ] && return 1                  # no owner yet (holder mid-acquire) -> treat as alive
  kill -0 "$pid" 2>/dev/null && return 1     # alive
  return 0                                   # dead
}
waited=0
until mkdir "$LOCK" 2>/dev/null; do
  if lock_holder_dead; then
    log "tick-lock holder (pid $(lock_owner_pid)) is dead — reclaiming"; rm -rf "$LOCK"; continue
  fi
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +15 2>/dev/null)" ]; then
    log "lock held >15 min (wedged/PID-reuse) — reclaiming"; rm -rf "$LOCK"; continue
  fi
  if [ "$waited" -ge "$LOCK_WAIT_S" ]; then
    log "lock still held by live pid $(lock_owner_pid) after ${waited}s (another tick) — skip"; exit 0
  fi
  sleep 1; waited=$((waited + 1))
done
printf '%s %s %s\n' "$$" planner "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$LOCK/owner" 2>/dev/null || true
[ "$waited" -gt 0 ] && log "acquired lock after ${waited}s wait"
trap 'rm -rf "$LOCK" 2>/dev/null' EXIT

{
  SECONDS=0
  log "=== tick start (LIVE armed=${LIVE_ARMED:-0} dd_model=${DD_MODEL}) ==="

  # 1) market regime (public data; appends to market_conditions.jsonl)
  log "▶ [1/5] market regime…"
  "$PYTHON" "${REPO}/scripts/market_conditions.py" --quiet || log "market_conditions failed (continuing)"

  # 2) Pull real broker state (buying power, positions, open orders) via the MCP relay.
  #    Fail-closed — if the snapshot can't be fetched, skip the whole tick.
  log "▶ [2/5] broker snapshot (live account read via relay; 30-180s)…"
  if ! "$PYTHON" "${REPO}/scripts/broker_snapshot.py" 2>>"$RUN_LOG" | tee -a "$RUN_LOG"; then
    log "broker_snapshot failed — failing closed, skipping this tick"
    log "=== tick end (${SECONDS}s) ==="
    exit 0
  fi

  # 3) context + gate — writes context/packet, prints GATE=...
  log "▶ [3/5] context + gate…"
  GATE_LINE="$("$PYTHON" "${REPO}/scripts/live_tick_context.py" 2>>"$RUN_LOG" | tail -n 1)"
  log "gate: ${GATE_LINE}"

  CTX="${REPO}/data/tick/context_latest.json"

  if [[ "$GATE_LINE" == GATE=SKIP* ]]; then
    # Skipped tick: live_execute still reconciles stops / books closures from broker snapshot.
    log "▶ [4/5] reconcile only (gate=skip — no decision)…"
    "$PYTHON" "$EXEC" --context "$CTX" --skip | tee -a "$RUN_LOG"
  else
    # 4) decide: Stage-1 screen (deterministic) -> Stage-2 deep DD + commit (Sonnet + web)
    log "▶ [4/5] deciding (Stage-2 DD + manage)…"
    DEC="${REPO}/data/tick/decision_latest.json"
    if "$PYTHON" "${REPO}/scripts/decide.py" 2>>"$RUN_LOG" | tee -a "$RUN_LOG"; then
      # 5) execute + log — re-checks caps, review->place via MCP relay
      log "▶ [5/5] executing (review → place via relay)…"
      "$PYTHON" "$EXEC" --context "$CTX" --decision "$DEC" | tee -a "$RUN_LOG"
    else
      log "decide.py failed — logging as skip"
      "$PYTHON" "$EXEC" --context "$CTX" --skip | tee -a "$RUN_LOG"
    fi
  fi
  log "=== tick end (${SECONDS}s) ==="
}
