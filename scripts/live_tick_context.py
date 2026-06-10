#!/usr/bin/env python3
"""
live_tick_context.py — deterministic context-gatherer for one trading tick (LIVE mode).

Thin live wrapper around tick_context.build_context(): loads broker truth from
data/tick/broker_snapshot.json + our stop/TP metadata from data/live_state.json, then
delegates to the shared context builder with mode="live".

live_execute.py owns start-of-day equity (live_state.json); this gatherer NEVER writes
paper_state.json. The broker snapshot must already be fresh when this runs — broker_snapshot.py
is called by run_live_tick.sh immediately before this script.

NOTE: the broker-snapshot portfolio (cash/buying_power) + positions parse is SHARED with
live_execute via live_snapshot.py — the single source of truth, so the cash/equity leg can no longer
drift between the gate and the executor (that drift once understated equity and tripped the breaker).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_snapshot as ls  # shared portfolio/positions parser (same source as the executor)
import market_conditions as mc
import tick_context as tc

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"


def env(key: str, default: str) -> str:
    v = os.environ.get(key)
    return v if v not in (None, "") else default


def load_live_state() -> dict:
    """Build a paper-state-shaped dict from broker truth so the shared context builder
    works unchanged. Cash + position qty/cost come from data/tick/broker_snapshot.json;
    stop/TP/entry_ts/scale metadata comes from data/live_state.json. We never write
    paper_state.json in live mode — live_execute.py owns live_state.json (incl. SOD equity).
    """
    snap_path = DATA / "tick" / "broker_snapshot.json"
    live_path = DATA / "live_state.json"
    snap = json.loads(snap_path.read_text()) if snap_path.exists() else {}
    lstate = json.loads(live_path.read_text()) if live_path.exists() else {}
    lots = lstate.get("lots", {})

    # Cash + raw position qty/cost come from the SHARED parser (live_snapshot) — the SAME source as
    # live_execute.parse_snapshot, so the cash/equity leg can't drift between the gate and the executor
    # (that drift understated equity and tripped the daily-loss breaker). We take the FULL cash leg as
    # the NAV cash; buying_power is only for spend, enforced downstream by live_execute's settled guard.
    cash = ls.parse_portfolio(snap)["cash"]

    positions: dict[str, dict] = {}
    for sym, rp in ls.parse_positions(snap).items():
        lot = lots.get(sym, {})
        entry = lot.get("entry_price") or rp.get("avg_cost") or 0.0
        positions[sym] = {
            "qty": rp["qty"], "entry_price": entry,
            "entry_ts": lot.get("entry_ts"),
            "stop_price": lot.get("stop_price"),
            "take_profit_price": lot.get("take_profit_price"),
            "high_water": lot.get("high_water"),  # trailing-stop peak (live_execute owns writes)
            "init_qty": lot.get("init_qty", rp["qty"]),
            "scaled": lot.get("scaled") or [],
            "stop_type": lot.get("stop_type", "synthetic"),
            # OG DD metadata persisted on the lot at entry (execute_buy): the Tier-1 risk monitor
            # reasons over conviction/hold_intent, and pead_qualified_at_entry tells the manage-DD
            # what CLASS of bet this was (today's gap can't re-measure the entry signal).
            "conviction": lot.get("conviction"),
            "hold_intent": lot.get("hold_intent"),
            "thesis_type": lot.get("thesis_type"),
            "pead_qualified": lot.get("pead_qualified"),
            "book": lot.get("book") or "disco",  # two-book split: lot ownership (v2 plan)
        }
    return {
        "cash": cash, "positions": positions, "realized_total": 0.0,
        "day": lstate.get("day"), "start_of_day_equity": lstate.get("start_of_day_equity"),
    }


def precheck() -> int:
    """Fast hours-only gate check — no I/O, no quote fetches, no broker snapshot.
    Prints GATE=SKIP:market_<session> or GATE=GO in milliseconds. Called by run_live_tick.sh
    before the expensive broker_snapshot step so out-of-hours ticks bail immediately."""
    from datetime import datetime, timezone
    now_et = datetime.now(timezone.utc).astimezone(mc.ET)
    session, is_open = mc.session_state(now_et)
    allow_offhours = env("ALLOW_OFFHOURS", "0") == "1"
    if not is_open and not allow_offhours:
        print(f"GATE=SKIP:market_{session}")
    else:
        print("GATE=GO")
    return 0


def main() -> int:
    return tc.main(state=load_live_state(), mode="live")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Live tick context gatherer.")
    ap.add_argument("--precheck", action="store_true",
                    help="fast hours gate only — no I/O, prints GATE=SKIP or GATE=GO")
    args = ap.parse_args()
    if args.precheck:
        raise SystemExit(precheck())
    raise SystemExit(main())
