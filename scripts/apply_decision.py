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
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trade_log  # shared trade-history writer (paper + live)

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
STATE_PATH = DATA / "paper_state.json"
ENGINE_LOG = DATA / "engine-log.jsonl"

# After the FIRST scale-out trim, ratchet the synthetic stop up to breakeven (entry) so the
# remaining shares can't turn a banked partial win into a net loser. 0/false = leave the stop put.
SCALE_BREAKEVEN = os.environ.get("SCALE_BREAKEVEN_AFTER_FIRST", "1").strip().lower() not in ("0", "false", "no", "")


def load_json(p: Path) -> dict:
    return json.loads(p.read_text())


def write_json_atomic(path: Path, obj) -> None:
    """Write JSON via temp + os.replace so a crash mid-write can't truncate the file.
    A truncated paper_state.json would wipe positions and silently re-baseline the
    daily-loss circuit breaker — so every state write goes through here."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


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


def decide_summary(filled: list, rejected: list, decision: dict, equity, day_pnl) -> str:
    """Final one-line tick summary — always explains WHY, so a HOLD is never a silent dead-end.

    Three cases, in priority order:
      - something filled / a cap rejected a proposed trade -> list each with its reason
      - nothing happened but the screen DID find names -> show the per-candidate DD verdict + reason
      - nothing happened and no candidates -> the screen rationale
    """
    parts = [f"{r['side'].upper()} {r.get('qty', '?')} {r['symbol']} @ {r.get('price', '?')}"
             f" — {str(r.get('reason', ''))[:60]}" for r in filled]
    # A post-commit cap rejection is its own important 'why': the LLM wanted in, a guardrail blocked it.
    rej = [f"{r['symbol']} {r['side']} REJECTED: {r.get('reject_reason')}" for r in rejected]
    if parts or rej:
        return (f"{len(filled)} filled, {len(rejected)} rejected | equity={equity} "
                f"day_pnl={day_pnl} | " + " ; ".join(parts + rej))
    # No actions at all -> the screen surfaced candidates but Stage-2 DD committed to none. Surface
    # each candidate's verdict + reason so the operator sees why no entry was taken.
    dd = decision.get("dd") or []
    if dd:
        why = " ; ".join(f"{d.get('symbol')} {(d.get('decision') or '?').upper()}: "
                         f"{(d.get('reason') or d.get('error') or '').strip()[:90]}" for d in dd)
        return f"HOLD — 0 of {len(dd)} candidate(s) committed: {why} | equity={equity}"
    return f"HOLD ({decision.get('rationale', 'no action')[:80]}) | equity={equity}"


def cand_last(context: dict, sym: str) -> float | None:
    for c in context.get("candidates", []):
        if c["symbol"] == sym:
            return c.get("last")
    for p in context.get("positions", []):
        if p["symbol"] == sym:
            return p.get("last")
    return None


def fill_price(ref: float, side: str, caps: dict) -> float:
    """Model adverse slippage on a paper fill: a buy pays UP through the touch, a sell gives up
    edge BELOW it, by SLIPPAGE_BPS of the reference quote. The old sim filled at the raw last
    (zero spread/slippage) which flatters P&L — this makes every fill honestly worse than the
    quote, which is the floor of what a real marketable order would get on the volatile universe."""
    bps = caps.get("SLIPPAGE_BPS", 0.0) / 10000.0
    return ref * (1 + bps) if side == "buy" else ref * (1 - bps)


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
        # HARD GUARD: never open a new position on stale / off-hours data, regardless of what the
        # LLM proposed. allow_entries is True only during regular hours with today's quote.
        if not context.get("allow_entries", False):
            result["reject_reason"] = f"entries disabled ({context.get('stale_reason') or 'market closed/stale'})"
            return result

        # --- marketable-limit entry (paper model of a real marketable BUY limit) ---
        # Cross the spread with a LIMIT capped at MARKETABLE_LIMIT_PCT above the touch (price
        # protection vs a runaway print), and fill at the slipped price. If the modeled fill would
        # print past the limit the order is NOT marketable -> no fill. This gate rarely bites in
        # same-tick paper (the quote can't drift between decide and fill), but it's the exact rule
        # the live executor will reuse, so it lives here and the limit is recorded on every entry.
        limit_price = round(price * (1 + caps.get("MARKETABLE_LIMIT_PCT", 0.5) / 100.0), 4)
        buy_px = fill_price(price, "buy", caps)
        if buy_px > limit_price + 1e-9:
            result["reject_reason"] = (f"not marketable: modeled fill {round(buy_px, 4)} > "
                                       f"limit {limit_price}")
            return result

        # Resolve quantity from dollar_amount or qty, SIZED OFF THE FILL PRICE (so slippage costs
        # shares, not hidden P&L). NaN/inf from a bad LLM dollar_amount is rejected before it can
        # sail past the cap comparisons and corrupt state.
        if action.get("dollar_amount") is not None:
            notional = float(action["dollar_amount"])
            qty = notional / buy_px
        elif action.get("qty") is not None:
            qty = float(action["qty"])
            notional = qty * buy_px
        else:
            result["reject_reason"] = "no qty/dollar_amount"
            return result
        if not (math.isfinite(qty) and math.isfinite(notional)) or qty <= 0 or notional <= 0:
            result["reject_reason"] = "non-positive / non-finite qty"
            return result

        # Hybrid stop eligibility: prefer a WHOLE-SHARE lot so it can carry a real resting
        # stop-market in live (Robinhood fractional lots are broker market-only -> synthetic engine
        # stop only). Floor to whole shares when >=1 is affordable; never force whole shares on a
        # name the budget can't reach a share of. Flooring only ever LOWERS notional, so no cap is
        # breached by it. Off (PREFER_WHOLE_SHARES=0) keeps pure fractional dollar sizing.
        if bool(caps.get("PREFER_WHOLE_SHARES", 1)) and action.get("dollar_amount") is not None \
                and math.floor(qty) >= 1:
            qty = float(math.floor(qty))
            notional = qty * buy_px

        min_pos = caps.get("MIN_POSITION_USD", 0.0)
        if min_pos > 0 and notional < min_pos - 1e-6:
            result["reject_reason"] = f"below MIN_POSITION_USD ({min_pos})"
            return result

        # --- cap checks (deterministic guardrails; every cap CLAUDE.md names is enforced here) ---
        existing_val = positions.get(sym, {}).get("qty", 0.0) * price
        if existing_val + notional > caps["MAX_POSITION_USD"] + 1e-6:
            result["reject_reason"] = f"exceeds MAX_POSITION_USD ({caps['MAX_POSITION_USD']})"
            return result
        # Exposure is valued at the live quote, falling back to entry_price ONLY when a held
        # quote is missing — and we take max(last, entry) so a missing quote can only *raise*
        # the exposure estimate (fail-safe: never under-count what we're risking).
        cur_exposure = sum(p["qty"] * max(cand_last(context, s) or 0.0, p["entry_price"])
                           for s, p in positions.items())
        if cur_exposure + notional > caps["MAX_TOTAL_EXPOSURE_USD"] + 1e-6:
            result["reject_reason"] = f"exceeds MAX_TOTAL_EXPOSURE_USD ({caps['MAX_TOTAL_EXPOSURE_USD']})"
            return result
        if sym not in positions and len(positions) >= caps["MAX_OPEN_POSITIONS"]:
            result["reject_reason"] = f"MAX_OPEN_POSITIONS ({caps['MAX_OPEN_POSITIONS']}) reached"
            return result
        # Per-name concentration is enforced by MAX_POSITION_USD above, which is itself a fraction of
        # live equity (MAX_POSITION_PCT) — so there is no separate symbol-weight cap. equity_now
        # (cash + live exposure) is still needed as the denominator for the at-fill daily-loss
        # re-check below.
        equity_now = state["cash"] + cur_exposure
        # Per-trade-loss budget: bound the dollar loss if the stop fills at stop_price. (This bounds
        # *sizing at the stop*, not realized loss — a synthetic stop can gap through; that's why
        # MAX_POSITION_USD and the EOD flatten also exist.)
        implied_stop_loss = notional * caps["STOP_LOSS_PCT"] / 100.0
        max_trade_loss = caps.get("MAX_PER_TRADE_LOSS_USD", 60.0)
        if implied_stop_loss > max_trade_loss + 1e-6:
            result["reject_reason"] = (f"exceeds MAX_PER_TRADE_LOSS_USD: "
                                       f"{round(implied_stop_loss, 2)} > {max_trade_loss}")
            return result
        # Re-check the daily-loss circuit breaker AT FILL (not just at the gate). The gate runs on
        # pre-tick equity; a buy that pushes day P&L past the limit must still be blocked.
        sod = state.get("start_of_day_equity")
        if sod is not None:
            day_pnl_now = equity_now - sod
            if day_pnl_now <= -caps.get("DAILY_MAX_LOSS_USD", 150.0):
                result["reject_reason"] = (f"circuit_breaker day_pnl={round(day_pnl_now, 2)} "
                                           f"<= -{caps.get('DAILY_MAX_LOSS_USD', 150.0)}")
                return result
        if notional > state["cash"] + 1e-6:
            result["reject_reason"] = f"insufficient cash ({round(state['cash'], 2)})"
            return result

        # --- fill + attach a protective stop / take-profit level ---
        # NOTE: this stop is SYNTHETIC — enforced by our engine only when it ticks
        # (~5 min) and the host is awake. It is NOT a resting broker stop order, so it
        # does NOT cover between-tick moves, overnight/pre-market gaps, or an
        # asleep/crashed engine. Treat it as best-effort intraday protection, not
        # broker-grade. See strategies/momentum-v0-plan.md (live = real resting stop).
        prev = positions.get(sym, {"qty": 0.0, "entry_price": buy_px})
        new_qty = prev["qty"] + qty
        new_entry = (prev["qty"] * prev["entry_price"] + qty * buy_px) / new_qty
        sl, tp = caps.get("STOP_LOSS_PCT", 2.0), caps.get("TAKE_PROFIT_PCT", 4.0)
        # Hybrid stop tag: a WHOLE-SHARE lot is resting-eligible (a real broker stop-market in live,
        # armed continuously); any fractional remainder (incl. when averaging in) forces synthetic.
        # NOTE: in PAPER both still sell at the next tick, so resting vs synthetic fill identically
        # here — the tag drives the live executor + record fidelity, and the protection edge
        # (continuous arming, surviving between-tick/overnight gaps and engine downtime) only
        # materializes once live places the resting order. See momentum-v0-plan.md.
        resting = (bool(caps.get("PREFER_WHOLE_SHARES", 1))
                   and new_qty == math.floor(new_qty) and new_qty >= 1)
        stop_type = "resting" if resting else "synthetic"
        stop_note = ("resting stop-market on whole-share lot (broker-armed in live; paper still "
                     "settles at tick)" if resting
                     else "engine-tick enforced (~5m); no gap/overnight/engine-down cover")
        positions[sym] = {"qty": new_qty, "entry_price": round(new_entry, 4),
                          "init_qty": round(new_qty, 6),  # scale-out base: fractions are of this entry qty
                          "entry_ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          "stop_price": round(new_entry * (1 - sl / 100), 4),
                          "take_profit_price": round(new_entry * (1 + tp / 100), 4),
                          "stop_type": stop_type,
                          # OG DD persisted for the Tier-1 hold-risk monitor (hold_risk.py); kept on averaging-in.
                          "conviction": action.get("conviction") or prev.get("conviction"),
                          "hold_intent": action.get("hold_intent") or prev.get("hold_intent"),
                          "thesis_type": action.get("thesis_type") or prev.get("thesis_type"),
                          "scaled": prev.get("scaled", [])}  # tiers already taken (preserved when averaging in)
        state["cash"] -= notional
        result.update(status="filled", qty=round(qty, 6), price=round(buy_px, 4),
                      ref_price=round(price, 4), order_type="marketable_limit",
                      limit_price=limit_price, slippage_bps=caps.get("SLIPPAGE_BPS", 0.0),
                      notional=round(notional, 2),
                      stop_price=positions[sym]["stop_price"],
                      take_profit_price=positions[sym]["take_profit_price"],
                      stop_type=stop_type, stop_note=stop_note)
        return result

    # side == "sell"
    if sym not in positions:
        result["reject_reason"] = "no position to sell"
        return result
    held = positions[sym]["qty"]
    lot_stop_type = positions[sym].get("stop_type")  # capture before a full exit deletes the lot
    # Exit fill: model adverse slippage on the way OUT. A risk-rule exit (stop / EOD flatten /
    # wind-down / max-hold) is a market or stop-market order — it must complete, so no limit cap;
    # a discretionary or scale-out sell is treated as a marketable limit. order_type is recorded on
    # the trail either way; slippage applies to all of them so paper exits aren't flattered.
    reason_l = reason.lower()
    if "stop" in reason_l:
        order_type = "stop_market"
    elif any(k in reason_l for k in ("flatten", "wind-down", "max-hold")):
        order_type = "market"
    else:
        order_type = "marketable_limit"
    sell_px = fill_price(price, "sell", caps)
    qty = float(action["qty"]) if action.get("qty") is not None else (
        float(action["dollar_amount"]) / sell_px if action.get("dollar_amount") is not None else held)
    qty = min(qty, held)
    if qty <= 0:
        result["reject_reason"] = "non-positive sell qty"
        return result
    proceeds = qty * sell_px
    realized = (sell_px - positions[sym]["entry_price"]) * qty
    state["cash"] += proceeds
    state["realized_total"] += realized
    remaining = held - qty
    scale_tiers = action.get("scale_tiers")
    if remaining <= 1e-9:
        del positions[sym]
        # Full exit: start the re-entry cooldown (anti-whipsaw). A partial scale-out does NOT —
        # the name is still held, and cooldown only governs re-ENTERING a name we've fully left.
        state.setdefault("last_exit", {})[sym] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    else:
        positions[sym]["qty"] = remaining
        if scale_tiers:
            # Mark these tiers taken so the screen won't re-trim them next tick, and after the
            # FIRST trim ratchet the synthetic stop up to breakeven (entry) to lock the runner.
            taken = positions[sym].get("scaled") or []
            first_trim = not taken
            positions[sym]["scaled"] = taken + [t for t in scale_tiers if t not in taken]
            if first_trim and SCALE_BREAKEVEN:
                entry = positions[sym]["entry_price"]
                if positions[sym].get("stop_price") is None or positions[sym]["stop_price"] < entry:
                    positions[sym]["stop_price"] = round(entry, 4)
    result.update(status="filled", qty=round(qty, 6), price=round(sell_px, 4),
                  ref_price=round(price, 4), order_type=order_type,
                  slippage_bps=caps.get("SLIPPAGE_BPS", 0.0), stop_type=lot_stop_type,
                  notional=round(proceeds, 2), realized_usd=round(realized, 2),
                  **({"scale_tiers": scale_tiers} if scale_tiers else {}))
    return result


def recompute_portfolio(state: dict, context: dict) -> tuple[float, float]:
    """(equity, day_pnl) valued at this tick's quotes — single source of truth for both branches."""
    pos_val = sum(p["qty"] * (cand_last(context, s) or p["entry_price"])
                  for s, p in state["positions"].items())
    equity = round(state["cash"] + pos_val, 2)
    day_pnl = round(equity - (state.get("start_of_day_equity") or equity), 2)
    return equity, day_pnl


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", required=True)
    ap.add_argument("--decision")
    ap.add_argument("--skip", action="store_true", help="log a skipped tick (no decision)")
    args = ap.parse_args()

    context = load_json(Path(args.context))
    caps = context["caps"]
    now = datetime.now(timezone.utc)

    # Defense in depth: this executor ONLY simulates paper fills — there is no
    # review_equity_order/place_equity_order path here. Refuse to run if mislabeled live.
    if str(context.get("mode", "paper")).lower() == "live":
        print("[apply_decision] FATAL: TRADING_MODE=live but no live executor exists — refusing.",
              file=sys.stderr)
        return 2

    state_path = STATE_PATH
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            # Corrupt state would silently re-baseline to a wiped portfolio AND reset the
            # circuit-breaker baseline. Back it up, log loudly, and refuse — never overwrite.
            bak = state_path.with_suffix(f".corrupt-{now.strftime('%Y%m%dT%H%M%S')}.json")
            try:
                os.replace(state_path, bak)
            except OSError:
                pass
            print(f"[apply_decision] FATAL: paper_state.json unreadable; backed up to {bak.name}",
                  file=sys.stderr)
            return 2
    else:
        state = {"cash": 0.0, "positions": {}, "realized_total": 0.0, "day": None,
                 "start_of_day_equity": None}

    gate_reason = context.get("gate_reason", "")
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
        # A circuit-breaker SKIP halts new ENTRIES — it must NOT strand open positions without
        # their protective stops. So on a breaker SKIP we still run the deterministic exits, but
        # only when the market is open with fresh data (never sell on a stale/closed quote).
        exit_results = []
        run_exits = (gate_reason.startswith("circuit_breaker")
                     and context.get("market_open") and not context.get("data_stale"))
        if run_exits:
            exits = (context.get("screen") or {}).get("exits") or []
            exit_actions = [{"symbol": e.get("symbol"), "side": "sell",
                             "reason": f"[breaker-exit] {e.get('reason', '')}",
                             **({"qty": e["qty"]} if e.get("qty") is not None else {}),
                             **({"scale_tiers": e["scale_tiers"]} if e.get("scale_tiers") else {})}
                            for e in exits if e.get("symbol")]
            exit_results = [validate_and_fill(a, context, state, caps) for a in exit_actions]
            write_json_atomic(state_path, state)
        equity, day_pnl = recompute_portfolio(state, context)
        filled = [r for r in exit_results if r["status"] == "filled"]
        record.update(
            action=("manage_exits" if run_exits else "skip"),
            gate_reason=gate_reason, rationale="", results=exit_results,
            n_filled=len(filled), n_rejected=len(exit_results) - len(filled),
            # Audit: keep enough context to reconstruct what a skipped tick saw (held positions,
            # the screen, whether entries were allowed) — half of all records are skips.
            allow_entries=context.get("allow_entries"),
            positions=context.get("positions"),
            screen=context.get("screen"),
            portfolio_after={"cash": round(state["cash"], 2), "equity": equity, "day_pnl": day_pnl,
                             "open_positions": len(state["positions"]),
                             "realized_total": round(state["realized_total"], 2)},
        )
        if filled:
            parts = [f"SELL {r.get('qty', '?')} {r['symbol']} @ {r.get('price', '?')}" for r in filled]
            summary = (f"BREAKER-EXIT {len(filled)} sold | equity={equity} day_pnl={day_pnl} | "
                       + " ; ".join(parts))
        else:
            summary = f"SKIP — {gate_reason or 'gated'} | equity={equity} day_pnl={day_pnl}"
    else:
        raw = Path(args.decision).read_text()
        decision = extract_decision(raw)
        results = [validate_and_fill(a, context, state, caps) for a in decision["actions"]]
        # Recompute equity post-fills with the same quotes used this tick.
        equity, day_pnl = recompute_portfolio(state, context)
        write_json_atomic(state_path, state)
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
        if isinstance(decision.get("dd"), list) and decision["dd"]:
            record["dd"] = decision["dd"]          # Stage-2 commit/reject DD + catalysts (audit)
        if decision.get("screen"):
            record["screen"] = decision["screen"]  # Stage-1 exits + entry candidates (audit)
        if decision.get("_parse_error"):
            record["parse_error"] = True
        summary = decide_summary(filled, rejected, decision, equity, day_pnl)

    ENGINE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ENGINE_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")

    # Mirror every executed fill to the unified trade history (data/trades.jsonl + daily blotter),
    # independent of this per-tick record. Best-effort: never let history I/O break a tick.
    trade_log.record_fills(record.get("results", []), ts_utc=record["ts_utc"],
                           ts_et=record.get("ts_et"), mode=record["mode"])

    print(f"[{record['ts_et']}] {record['mode'].upper()} {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
