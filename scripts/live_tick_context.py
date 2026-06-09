#!/usr/bin/env python3
"""
live_tick_context.py — deterministic context-gatherer for one trading tick (LIVE mode).

Thin live wrapper around tick_context.build_context(): loads broker truth from
data/tick/broker_snapshot.json + our stop/TP metadata from data/live_state.json, then
delegates to the shared context builder with mode="live".

live_execute.py owns start-of-day equity (live_state.json); this gatherer NEVER writes
paper_state.json. The broker snapshot must already be fresh when this runs — broker_snapshot.py
is called by run_live_tick.sh immediately before this script.

NOTE: load_live_state() parses the same broker_snapshot.json as live_execute.parse_snapshot.
Keep the two field mappings in sync (or extract to a shared live_snapshot.py if they diverge).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
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

    # buying power — confirmed live shape: data.buying_power.buying_power (nested), fallback data.cash.
    # Tool results may arrive wrapped as {"data": {...}}; peel that first. Same mapping as
    # live_execute.parse_snapshot — keep the two in sync.
    def _flt(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    pf = snap.get("portfolio") or {}
    if isinstance(pf, dict) and isinstance(pf.get("data"), dict):
        pf = pf["data"]
    bp = pf.get("buying_power") if isinstance(pf, dict) else None
    cash = _flt(bp.get("buying_power")) if isinstance(bp, dict) else _flt(bp)
    if cash is None:
        cash = _flt(pf.get("cash")) or 0.0

    raw_pos = snap.get("positions")
    if isinstance(raw_pos, dict):
        raw_pos = raw_pos.get("data", raw_pos)
        if isinstance(raw_pos, dict):
            raw_pos = raw_pos.get("positions") or raw_pos.get("results") or []
    positions: dict[str, dict] = {}
    for p in raw_pos or []:
        if not isinstance(p, dict):
            continue
        sym = str(p.get("symbol") or p.get("ticker") or "").upper().strip()
        try:
            qty = float(p.get("quantity") or p.get("qty") or p.get("shares") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if not sym or qty <= 0:
            continue
        avg = (p.get("average_buy_price") or p.get("average_price")
               or p.get("avg_cost") or p.get("price"))
        try:
            entry = float(avg) if avg is not None else None
        except (TypeError, ValueError):
            entry = None
        lot = lots.get(sym, {})
        entry = lot.get("entry_price") or entry or 0.0
        positions[sym] = {
            "qty": qty, "entry_price": entry,
            "entry_ts": lot.get("entry_ts"),
            "stop_price": lot.get("stop_price"),
            "take_profit_price": lot.get("take_profit_price"),
            "high_water": lot.get("high_water"),  # trailing-stop peak (live_execute owns writes)
            "init_qty": lot.get("init_qty", qty),
            "scaled": lot.get("scaled") or [],
            "stop_type": lot.get("stop_type", "synthetic"),
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
