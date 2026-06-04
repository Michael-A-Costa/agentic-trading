#!/usr/bin/env bash
#
# run_trading_tick.sh — one trading tick (PAPER mode), driven by launchd/cron.
#
# Flow: lock -> market regime (script) -> context+gate (script) -> [LLM decision] -> apply+log (script).
# The LLM is invoked ONLY for the decision; all gathering and logging are scripts, so a tick costs
# just the decision tokens. Single-flight via a lock dir so ticks never overlap.
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO" || exit 1

# Load config (.env preferred, else .env.example defaults).
set -a
[ -f "$REPO/.env" ] && . "$REPO/.env" || { [ -f "$REPO/.env.example" ] && . "$REPO/.env.example"; }
set +a

PYTHON="${AGENTIC_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
CLAUDE_BIN="${AGENTIC_CLAUDE:-$(command -v claude || echo "$HOME/.local/bin/claude")}"
TICK_MODEL="${TICK_MODEL:-claude-haiku-4-5-20251001}"

LOG_DIR="${REPO}/data/logs"; mkdir -p "$LOG_DIR"
RUN_LOG="${LOG_DIR}/tick_$(date +%Y-%m-%d).log"
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$RUN_LOG"; }

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
  log "=== tick start (mode=${TRADING_MODE:-paper} model=${TICK_MODEL}) ==="

  # 1) market regime (public data; appends to market_conditions.jsonl)
  "$PYTHON" "${REPO}/scripts/market_conditions.py" --quiet || log "market_conditions failed (continuing)"

  # 2) context + gate (script) — writes context/packet, prints GATE=...
  GATE_LINE="$("$PYTHON" "${REPO}/scripts/tick_context.py" | tail -n 1)"
  log "gate: ${GATE_LINE}"

  CTX="${REPO}/data/tick/context_latest.json"

  if [[ "$GATE_LINE" == GATE=SKIP* ]]; then
    # log the skipped tick deterministically; no LLM call
    "$PYTHON" "${REPO}/scripts/apply_decision.py" --context "$CTX" --skip | tee -a "$RUN_LOG"
  else
    # 3) decide: Stage-1 screen (cheap) -> Stage-2 deep DD + commit (Opus + web) on candidates
    DEC="${REPO}/data/tick/decision_latest.json"
    if "$PYTHON" "${REPO}/scripts/decide.py" 2>>"$RUN_LOG" | tee -a "$RUN_LOG"; then
      # 4) apply + log (script) — re-checks caps, simulates fills, writes the what+why record
      "$PYTHON" "${REPO}/scripts/apply_decision.py" --context "$CTX" --decision "$DEC" | tee -a "$RUN_LOG"
    else
      log "decide.py failed — logging as skip"
      "$PYTHON" "${REPO}/scripts/apply_decision.py" --context "$CTX" --skip | tee -a "$RUN_LOG"
    fi
  fi
  log "=== tick end ==="
}

# log() and the apply_decision `tee` already append to RUN_LOG; their stdout copy lands in
# launchd.out (real-time visibility). No outer RUN_LOG redirect, so nothing is double-written.
