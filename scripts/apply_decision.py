#!/usr/bin/env python3
"""
apply_decision.py — deterministic executor + logger for one trading tick (PAPER mode).

Takes the LLM's decision JSON and the context packet, then does everything the LLM must NOT
be trusted to do itself:
  - re-validates every action against the .env risk caps (the LLM is advisory; this is the gate)
  - simulates fills at the current public quote (paper) and updates data/paper_state.json
  - writes a complete, human-readable "what + why" record to data/engine-log.jsonl
  - prints a concise tick summary

All logging lives here (a script), not in the LLM — so each tick costs only the decision tokens.

Usage:
  apply_decision.py --context data/tick/context_latest.json --decision data/tick/decision_latest.json
  apply_decision.py --context data/tick/context_latest.json --skip      # log a skipped tick
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
STATE_PATH = DATA / "paper_state.json"
ENGINE_LOG = DATA / "engine-log.jsonl"


def load_json(p: Path) -> dict:
    return json.loads(p.read_text())


def extract_decision(raw: str) -> dict:
    """Pull a JSON object out of the model's text (tolerates ```json fences / surrounding prose)."""
    raw = raw.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        raw = m.group(1)
    else:
        a, b = raw.find("{"), raw.rfind("}")
        if a != -1 and b != -1 and b > a:
            raw = raw[a:b + 1]
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return {"actions": [], "rationale": "", "_parse_error": True}
    if not isinstance(d.get("actions"), list):
        d["actions"] = []
    return d


def cand_last(context: dict, sym: str) -> float | None:
    for c in context.get("candidates", []):
        if c["symbol"] == sym:
            return c.get("last")
    for p in context.get("positions", []):
        if p["symbol"] == sym:
            return p.get("last")
    return None


def validate_and_fill(action: dict, context: dict, state: dict, caps: dict) -> dict:
    """Validate one action against caps and (if ok) simulate a paper fill, mutating state."""
    sym = str(action.get("symbol", "")).upper().strip()
    side = str(action.get("side", "")).lower().strip()
    reason = str(action.get("reason", "")).strip()
    result: dict[str, object] = {"symbol": sym, "side": side, "reason": reason, "status": "rejected"}

    if not sym or side not in ("buy", "sell"):
        result["reject_reason"] = "bad symbol/side"
        return result
    price = cand_last(context, sym)
    if not price or price <= 0:
        result["reject_reason"] = "no quote for symbol"
        return result

    positions = state["positions"]

    if side == "buy":
        # Resolve quantity from dollar_amount or qty.
        if action.get("dollar_amount") is not None:
            notional = float(action["dollar_amount"])
            qty = notional / price
        elif action.get("qty") is not None:
            qty = float(action["qty"])
            notional = qty * price
        else:
            result["reject_reason"] = "no qty/dollar_amount"
            return result
        if qty <= 0:
            result["reject_reason"] = "non-positive qty"
            return result

        # --- cap checks (deterministic guardrails) ---
        existing_val = positions.get(sym, {}).get("qty", 0.0) * price
        if existing_val + notional > caps["MAX_POSITION_USD"] + 1e-6:
            result["reject_reason"] = f"exceeds MAX_POSITION_USD ({caps['MAX_POSITION_USD']})"
            return result
        cur_exposure = sum(p["qty"] * (cand_last(context, s) or p["entry_price"])
                           for s, p in positions.items())
        if cur_exposure + notional > caps["MAX_TOTAL_EXPOSURE_USD"] + 1e-6:
            result["reject_reason"] = f"exceeds MAX_TOTAL_EXPOSURE_USD ({caps['MAX_TOTAL_EXPOSURE_USD']})"
            return result
        if sym not in positions and len(positions) >= caps["MAX_OPEN_POSITIONS"]:
            result["reject_reason"] = f"MAX_OPEN_POSITIONS ({caps['MAX_OPEN_POSITIONS']}) reached"
            return result
        if notional > state["cash"] + 1e-6:
            result["reject_reason"] = f"insufficient cash ({round(state['cash'], 2)})"
            return result

        # --- fill ---
        prev = positions.get(sym, {"qty": 0.0, "entry_price": price})
        new_qty = prev["qty"] + qty
        new_entry = (prev["qty"] * prev["entry_price"] + qty * price) / new_qty
        positions[sym] = {"qty": new_qty, "entry_price": round(new_entry, 4),
                          "entry_ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        state["cash"] -= notional
        result.update(status="filled", qty=round(qty, 6), price=price,
                      notional=round(notional, 2))
        return result

    # side == "sell"
    if sym not in positions:
        result["reject_reason"] = "no position to sell"
        return result
    held = positions[sym]["qty"]
    qty = float(action["qty"]) if action.get("qty") is not None else (
        float(action["dollar_amount"]) / price if action.get("dollar_amount") is not None else held)
    qty = min(qty, held)
    if qty <= 0:
        result["reject_reason"] = "non-positive sell qty"
        return result
    proceeds = qty * price
    realized = (price - positions[sym]["entry_price"]) * qty
    state["cash"] += proceeds
    state["realized_total"] += realized
    remaining = held - qty
    if remaining <= 1e-9:
        del positions[sym]
    else:
        positions[sym]["qty"] = remaining
    result.update(status="filled", qty=round(qty, 6), price=price,
                  notional=round(proceeds, 2), realized_usd=round(realized, 2))
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", required=True)
    ap.add_argument("--decision")
    ap.add_argument("--skip", action="store_true", help="log a skipped tick (no decision)")
    args = ap.parse_args()

    context = load_json(Path(args.context))
    caps = context["caps"]
    now = datetime.now(timezone.utc)

    state_path = STATE_PATH
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "cash": 0.0, "positions": {}, "realized_total": 0.0, "day": None, "start_of_day_equity": None}

    record = {
        "ts_utc": now.isoformat(timespec="seconds"),
        "ts_et": context.get("ts_et"),
        "mode": context.get("mode", "paper"),
        "session": context.get("session"),
        "quote_source": context.get("quote_source"),
        "regime": context.get("regime", {}).get("posture"),
        "regime_full": context.get("regime"),
        "portfolio_before": context.get("portfolio"),
    }

    if args.skip or not args.decision:
        record.update(action="skip", gate_reason=context.get("gate_reason", ""),
                      results=[], rationale="")
        summary = f"SKIP — {context.get('gate_reason', 'gated')}"
    else:
        raw = Path(args.decision).read_text()
        decision = extract_decision(raw)
        results = [validate_and_fill(a, context, state, caps) for a in decision["actions"]]
        # Recompute equity post-fills with the same quotes used this tick.
        pos_val = sum(p["qty"] * (cand_last(context, s) or p["entry_price"])
                      for s, p in state["positions"].items())
        equity = round(state["cash"] + pos_val, 2)
        day_pnl = round(equity - (state.get("start_of_day_equity") or equity), 2)
        state_path.write_text(json.dumps(state, indent=2))
        filled = [r for r in results if r["status"] == "filled"]
        rejected = [r for r in results if r["status"] != "filled"]
        record.update(
            action="decide",
            rationale=decision.get("rationale", ""),
            results=results,
            n_filled=len(filled), n_rejected=len(rejected),
            portfolio_after={"cash": round(state["cash"], 2), "equity": equity,
                             "day_pnl": day_pnl, "open_positions": len(state["positions"]),
                             "realized_total": round(state["realized_total"], 2)},
        )
        if decision.get("_parse_error"):
            record["parse_error"] = True
        parts = [f"{r['side'].upper()} {r.get('qty', '?')} {r['symbol']} @ {r.get('price', '?')}"
                 f" — {r['reason'][:60]}" for r in filled]
        rej = [f"{r['symbol']} {r['side']} rejected: {r.get('reject_reason')}" for r in rejected]
        summary = (f"{len(filled)} filled, {len(rejected)} rejected | equity={equity} "
                   f"day_pnl={day_pnl} | " + " ; ".join(parts + rej) if (parts or rej)
                   else f"HOLD ({decision.get('rationale', 'no action')[:80]}) | equity={equity}")

    ENGINE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ENGINE_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")

    print(f"[{record['ts_et']}] {record['mode'].upper()} {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
