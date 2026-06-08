#!/usr/bin/env python3
"""
sentinel.py — the FAST loop of the two-rate control architecture (PAPER mode), run every ~1 min.

It is the deterministic, NO-LLM half of the system (see docs/two-rate-architecture.md):
  - fires every protective EXIT the deterministic screen surfaces — synthetic stops, take-profits,
    Tier-1 risk soft-cuts, scale-out tiers, time-exits — at 1-min latency instead of the planner's
    5-min cadence, and
  - fires any ARMED ENTRY whose price trigger has crossed (the planner set it; the sentinel pulls
    the trigger), so a level entry happens the minute it triggers without an ~85s DD in the hot path.

It shares tick_context.build_context() (identical exit rules + caps as the planner) and
apply_decision.validate_and_fill() (identical cap gate + paper fill) — so a sentinel action is
indistinguishable from a planner action except for cadence and the source tag. It NEVER calls the
model or touches the MCP. Its quote fetch runs lock-free; only the read-modify-write of
paper_state.json is wrapped in the short-held data/.state.lock (shared with apply_decision), so the
planner's DD never starves the sentinel and the two loops can't lose each other's writes.

Usage:  sentinel.py            # one fast pass
        sentinel.py --dry-run  # evaluate + print intended actions, mutate/write NOTHING
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tick_context as tc
import apply_decision as ad
import trade_log
from state_lock import state_lock  # short critical section shared with apply_decision

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
STATE_PATH = DATA / "paper_state.json"
ENGINE_LOG = DATA / "engine-log.jsonl"


def exit_actions_from_screen(context: dict) -> list[dict]:
    """Turn the deterministic screen's exits into sell actions, tagged so the blotter shows the
    sentinel fired them. Same shape the planner's breaker-exit path builds."""
    exits = (context.get("screen") or {}).get("exits") or []
    return [{"symbol": e["symbol"], "side": "sell",
             "reason": f"[sentinel] {e.get('reason', '')}",
             **({"qty": e["qty"]} if e.get("qty") is not None else {}),
             **({"scale_tiers": e["scale_tiers"]} if e.get("scale_tiers") else {})}
            for e in exits if e.get("symbol")]


def armed_entry_actions(state: dict, context: dict, now: datetime) -> tuple[list[dict], list[str]]:
    """Check each armed entry against the fresh quote. Returns (buy actions to fire, symbols to drop).

    Drop = expired, or a malformed/garbage entry. Fire = trigger crossed AND entries are allowed
    (market open, fresh data, not gated near the close). A crossed trigger while entries are gated is
    kept armed and retried next minute. validate_and_fill re-checks the full cap gate at fire time.
    """
    armed = state.get("armed_entries") or {}
    fire: list[dict] = []
    drop: list[str] = []
    allow = bool(context.get("allow_entries"))
    for sym, a in list(armed.items()):
        exp = a.get("expires_ts")
        try:
            if exp and now > datetime.fromisoformat(exp):
                drop.append(sym)
                continue
        except (ValueError, TypeError):
            drop.append(sym)
            continue
        last = ad.cand_last(context, sym)
        trig, direction = a.get("trigger_price"), a.get("direction")
        if last is None or trig is None:
            continue  # no fresh quote this minute -> keep armed, try again
        crossed = (direction == "above" and last >= trig) or (direction == "below" and last <= trig)
        if not crossed or not allow:
            continue
        fire.append({"symbol": sym, "side": "buy",
                     "reason": f"[sentinel armed] {a.get('reason', '')}",
                     **({"dollar_amount": a["dollar_amount"]} if a.get("dollar_amount") is not None else {}),
                     **({"qty": a["qty"]} if a.get("qty") is not None else {}),
                     "conviction": a.get("conviction"), "hold_intent": a.get("hold_intent"),
                     "thesis_type": a.get("thesis_type")})
    return fire, drop


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="evaluate + print intended actions; mutate/write nothing")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    # monitor scope: fetch ONLY held + armed + indexes (no discovery/pins). The sentinel runs exits +
    # armed-trigger checks, never screens new names, so a lean ~12-symbol fetch keeps us under Cboe's
    # per-IP rate limit every minute instead of bursting the full 30-40 universe.
    context = tc.build_context(now, scope="monitor")  # quote fetch — OUTSIDE the state lock (slow, read-only)

    # Live exits run through the broker relay + resting stops, not this paper executor.
    if str(context.get("mode", "paper")).lower() == "live":
        return 0
    # Exits and entries both need a fresh, regular-hours quote to act on. Off-hours / stale ->
    # nothing to do (the planner's gate logs the skip; the sentinel stays silent to avoid noise).
    if not context.get("market_open") or context.get("data_stale"):
        return 0

    caps = context["caps"]
    # Circuit breaker: like the planner, a tripped breaker still runs exits (never strand a position
    # without protection) but fires NO new entries.
    breaker = (context.get("gate_reason") or "").startswith("circuit_breaker")

    if args.dry_run:
        state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else None
        if state is None:
            print("[sentinel] no paper_state.json yet — nothing to manage")
            return 0
        actions = exit_actions_from_screen(context)
        fire, drop = armed_entry_actions(state, context, now)
        actions += [] if breaker else fire
        print(f"[sentinel DRY-RUN] {context.get('ts_et')} breaker={breaker} "
              f"exits={len(exit_actions_from_screen(context))} arm_fire={0 if breaker else len(fire)} "
              f"arm_drop={0 if breaker else len(drop)}")
        for a in actions:
            print(f"  {a['side'].upper()} {a['symbol']} "
                  f"{a.get('qty') or a.get('dollar_amount') or 'ALL'} — {a.get('reason')}")
        return 0

    # The read -> mutate -> write below is the critical section, held for milliseconds under
    # .state.lock (shared with apply_decision). build_context's quote fetch already happened ABOVE,
    # outside this lock, so a slow fetch never blocks the planner and vice-versa.
    with state_lock():
        state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else None
        if state is None:
            print("[sentinel] no paper_state.json yet — nothing to manage")
            return 0
        actions = exit_actions_from_screen(context)
        fire, drop = armed_entry_actions(state, context, now)
        if breaker:
            drop = []  # leave armed entries in place; the breaker just suppresses firing this pass
        else:
            actions += fire
        if not actions and not drop:
            return 0  # quiet minute: nothing crossed, nothing to expire

        results = [ad.validate_and_fill(a, context, state, caps) for a in actions]
        # Consume fired armed entries + expired ones (a filled buy means the trigger did its job).
        armed_map = state.get("armed_entries") or {}
        for r in results:
            if r.get("status") == "filled" and r.get("side") == "buy":
                armed_map.pop(r["symbol"], None)
        for sym in drop:
            armed_map.pop(sym, None)

        equity, day_pnl = ad.recompute_portfolio(state, context)
        ad.write_json_atomic(STATE_PATH, state)
        filled = [r for r in results if r.get("status") == "filled"]
        record = {
            "ts_utc": now.isoformat(timespec="seconds"),
            "ts_et": context.get("ts_et"),
            "mode": context.get("mode", "paper"),
            "source": "sentinel",          # distinguishes fast-loop fills from planner fills in the log
            "action": "sentinel",
            "quote_source": context.get("quote_source"),
            "gate_reason": context.get("gate_reason", ""),
            "results": results,
            "n_filled": len(filled),
            "n_rejected": len(results) - len(filled),
            "armed_dropped": drop,
            "portfolio_after": {"cash": round(state["cash"], 2), "equity": equity, "day_pnl": day_pnl,
                                "open_positions": len(state["positions"]),
                                "realized_total": round(state.get("realized_total", 0.0), 2)},
        }

    # Append-only logging + print happen OUTSIDE the lock (no state mutation).
    ENGINE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ENGINE_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")
    trade_log.record_fills(results, ts_utc=record["ts_utc"], ts_et=record["ts_et"],
                           mode=record["mode"])
    if filled:
        parts = [f"{r['side'].upper()} {r.get('qty', '?')} {r['symbol']} @ {r.get('price', '?')}"
                 for r in filled]
        print(f"[{record['ts_et']}] equity={equity} SENTINEL {len(filled)} filled | "
              f"day_pnl={day_pnl} | " + " ; ".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
