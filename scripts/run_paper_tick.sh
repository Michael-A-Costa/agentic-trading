#!/usr/bin/env bash
#
# run_paper_tick.sh — one PAPER trading tick, driven by launchd/cron.
#
# PAPER mode only. This script never places real orders. All fills are simulated in
# apply_decision.py and tracked in data/paper_state.json.
#
# Flow: lock -> market regime -> context+gate (tick_context.py) -> [LLM decision] -> apply+log
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO" || exit 1

# Load config (.env preferred, else .env.example defaults), then FORCE paper mode regardless
# of whatever TRADING_MODE is set to in .env — the mode is determined by which script you run.
set -a
[ -f "$REPO/.env" ] && . "$REPO/.env" || { [ -f "$REPO/.env.example" ] && . "$REPO/.env.example"; }
TRADING_MODE=paper
set +a

PYTHON="${AGENTIC_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
DD_MODEL="${DD_MODEL:-claude-sonnet-4-6}"

LOG_DIR="${REPO}/data/logs"; mkdir -p "$LOG_DIR"
RUN_LOG="${LOG_DIR}/paper-tick_$(date +%Y-%m-%d).log"
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$RUN_LOG"; }

EXEC="${REPO}/scripts/apply_decision.py"

# --- single-flight lock (atomic mkdir); treat >15-min-old lock as stale ---
LOCK="${REPO}/data/.tick.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +15 2>/dev/null)" ]; then
    log "stale lock — reclaiming"; rmdir "$LOCK" 2>/dev/null; mkdir "$LOCK" 2>/dev/null || exit 0
  else
    log "another tick is running — skip"; exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  SECONDS=0
  log "=== tick start (PAPER dd_model=${DD_MODEL}) ==="

  # 1) market regime (public data; appends to market_conditions.jsonl)
  "$PYTHON" "${REPO}/scripts/market_conditions.py" --quiet || log "market_conditions failed (continuing)"

  # 2) context + gate — writes context/packet, prints GATE=...
  GATE_LINE="$("$PYTHON" "${REPO}/scripts/tick_context.py" | tail -n 1)"
  log "gate: ${GATE_LINE}"

  CTX="${REPO}/data/tick/context_latest.json"

  if [[ "$GATE_LINE" == GATE=SKIP* ]]; then
    "$PYTHON" "$EXEC" --context "$CTX" --skip | tee -a "$RUN_LOG"
  else
    # 3) decide: Stage-1 screen (deterministic) -> Stage-2 deep DD + commit (Sonnet + web)
    DEC="${REPO}/data/tick/decision_latest.json"
    if "$PYTHON" "${REPO}/scripts/decide.py" 2>>"$RUN_LOG" | tee -a "$RUN_LOG"; then
      # 4) execute + log — re-checks caps, simulates fill, updates paper_state.json
      "$PYTHON" "$EXEC" --context "$CTX" --decision "$DEC" | tee -a "$RUN_LOG"
    else
      log "decide.py failed — logging as skip"
      "$PYTHON" "$EXEC" --context "$CTX" --skip | tee -a "$RUN_LOG"
    fi
  fi
  log "=== tick end (${SECONDS}s) ==="
}
