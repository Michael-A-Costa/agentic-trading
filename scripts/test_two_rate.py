#!/usr/bin/env python3
"""
test_two_rate.py — unit coverage for the two-rate loop's reusable pieces, using a SYNTHETIC context
so it runs anytime (no network, no market-hours gate). Exercises: arming via apply_decision, the
sentinel's armed-entry trigger logic (cross / no-cross / expiry), exit extraction, the shared
validate_and_fill execution, and the double-entry guard. Run: python3 scripts/test_two_rate.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apply_decision as ad
import sentinel as sen

CAPS = {
    "SLIPPAGE_BPS": 10.0, "MARKETABLE_LIMIT_PCT": 0.5, "MIN_POSITION_USD": 0.0,
    "MAX_POSITION_USD": 1000.0, "MAX_TOTAL_EXPOSURE_USD": 5000.0, "MAX_OPEN_POSITIONS": 10,
    "STOP_LOSS_PCT": 4.0, "TAKE_PROFIT_PCT": 4.0, "MAX_PER_TRADE_LOSS_USD": 60.0,
    "DAILY_MAX_LOSS_USD": 150.0, "PREFER_WHOLE_SHARES": 1,
}
NOW = datetime(2026, 6, 5, 14, 0, tzinfo=timezone.utc)
PASS = FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    PASS += cond
    FAIL += not cond


def fresh_state(cash: float = 1000.0) -> dict:
    return {"cash": cash, "positions": {}, "realized_total": 0.0, "day": "2026-06-05",
            "start_of_day_equity": cash, "last_exit": {}, "armed_entries": {}}


def ctx(candidates=None, positions=None, allow_entries=True) -> dict:
    return {"mode": "paper", "market_open": True, "data_stale": False,
            "allow_entries": allow_entries, "candidates": candidates or [], "positions": positions or [],
            "caps": CAPS, "ts_et": "2026-06-05T10:00:00-04:00", "gate_reason": "",
            "screen": {"exits": []}, "quote_source": "test"}


print("ARMING (apply_decision.process_action)")
st = fresh_state()
action = {"symbol": "AAPL", "side": "buy", "arm": True, "dollar_amount": 100,
          "entry_trigger": {"price": 315.0, "direction": "above"}, "conviction": "high",
          "reason": "pre-WWDC breakout"}
r = ad.process_action(action, ctx(), st, CAPS, NOW)
check("returns status 'armed'", r.get("status") == "armed")
check("stashed in state.armed_entries", "AAPL" in st["armed_entries"])
check("trigger persisted", st["armed_entries"]["AAPL"]["trigger_price"] == 315.0)
check("immediate buy still fills (no entry_trigger)",
      ad.process_action({"symbol": "MSFT", "side": "buy", "dollar_amount": 50, "reason": "x"},
                        ctx(candidates=[{"symbol": "MSFT", "last": 400.0}]), fresh_state(), CAPS, NOW
                        ).get("status") == "filled")
bad = ad.process_action({"symbol": "X", "side": "buy", "arm": True, "dollar_amount": 10,
                         "entry_trigger": {"price": -1, "direction": "above"}}, ctx(), fresh_state(), CAPS, NOW)
check("rejects a malformed trigger", bad.get("status") == "rejected")

print("\nARMED-ENTRY TRIGGER (sentinel.armed_entry_actions)")
st = fresh_state()
ad.process_action(action, ctx(), st, CAPS, NOW)  # arm AAPL @ 315 above
fire, drop = sen.armed_entry_actions(st, ctx(candidates=[{"symbol": "AAPL", "last": 316.0}]), NOW)
check("fires when price crosses above trigger", len(fire) == 1 and fire[0]["symbol"] == "AAPL")
fire, drop = sen.armed_entry_actions(st, ctx(candidates=[{"symbol": "AAPL", "last": 314.0}]), NOW)
check("does NOT fire below trigger", len(fire) == 0 and not drop)
fire, drop = sen.armed_entry_actions(
    st, ctx(candidates=[{"symbol": "AAPL", "last": 316.0}], allow_entries=False), NOW)
check("crossed but entries gated -> hold armed (no fire, no drop)", not fire and not drop)
st_exp = fresh_state()
ad.process_action(action, ctx(), st_exp, CAPS, NOW)
st_exp["armed_entries"]["AAPL"]["expires_ts"] = (NOW - timedelta(minutes=1)).isoformat()
fire, drop = sen.armed_entry_actions(st_exp, ctx(candidates=[{"symbol": "AAPL", "last": 316.0}]), NOW)
check("expired trigger -> dropped, not fired", drop == ["AAPL"] and not fire)
# 'below' (pullback/limit) direction
st_b = fresh_state()
ad.process_action({"symbol": "NVDA", "side": "buy", "arm": True, "dollar_amount": 100,
                   "entry_trigger": {"price": 120.0, "direction": "below"}}, ctx(), st_b, CAPS, NOW)
fire, _ = sen.armed_entry_actions(st_b, ctx(candidates=[{"symbol": "NVDA", "last": 119.0}]), NOW)
check("'below' fires on a pullback to trigger", len(fire) == 1)

print("\nFIRED ENTRY EXECUTES (validate_and_fill)")
st = fresh_state(cash=1000.0)
ad.process_action(action, ctx(), st, CAPS, NOW)
fire, _ = sen.armed_entry_actions(st, ctx(candidates=[{"symbol": "AAPL", "last": 316.0}]), NOW)
res = ad.validate_and_fill(fire[0], ctx(candidates=[{"symbol": "AAPL", "last": 316.0}]), st, CAPS)
check("armed buy fills under the cap gate", res.get("status") == "filled")
check("position opened", "AAPL" in st["positions"] and st["positions"]["AAPL"]["qty"] > 0)
check("cash debited", st["cash"] < 1000.0)

print("\nEXIT FIRING (exit_actions_from_screen + validate_and_fill)")
st = fresh_state(cash=0.0)
st["positions"]["AAPL"] = {"qty": 10.0, "entry_price": 100.0, "init_qty": 10.0,
                           "stop_price": 96.0, "take_profit_price": 104.0, "stop_type": "synthetic",
                           "scaled": [], "entry_ts": "2026-06-05T13:00:00+00:00"}
exit_ctx = ctx(positions=[{"symbol": "AAPL", "last": 95.0}])
exit_ctx["screen"]["exits"] = [{"symbol": "AAPL", "reason": "synthetic stop hit: 95 <= stop 96 (-5%)"}]
acts = sen.exit_actions_from_screen(exit_ctx)
check("screen exit -> one tagged sell action", len(acts) == 1 and acts[0]["side"] == "sell"
      and acts[0]["reason"].startswith("[sentinel]"))
res = ad.validate_and_fill(acts[0], exit_ctx, st, CAPS)
check("stop exit fills (order_type stop_market)", res.get("status") == "filled"
      and res.get("order_type") == "stop_market")
check("position closed", "AAPL" not in st["positions"])
check("re-entry cooldown stamped", "AAPL" in st.get("last_exit", {}))

print("\nDOUBLE-ENTRY GUARD (immediate buy supersedes a pending arm)")
st = fresh_state()
ad.process_action(action, ctx(), st, CAPS, NOW)                       # arm AAPL
# simulate the planner immediately buying AAPL this tick, then the guard
buy = ad.validate_and_fill({"symbol": "AAPL", "side": "buy", "dollar_amount": 100, "reason": "now"},
                           ctx(candidates=[{"symbol": "AAPL", "last": 300.0}]), st, CAPS)
armed_map = st.get("armed_entries") or {}
for rr in [buy]:
    if rr.get("status") == "filled" and rr.get("side") == "buy":
        armed_map.pop(rr["symbol"], None)
check("filled immediate buy clears the pending arm", "AAPL" not in st["armed_entries"])

print("\nINTEGRATION (sentinel.main end-to-end, monkeypatched context + temp state)")
import json as _json
import tempfile
tmp = Path(tempfile.mkdtemp())
state_file, log_file = tmp / "paper_state.json", tmp / "engine.jsonl"
istate = fresh_state(cash=0.0)
istate["positions"]["AAPL"] = {"qty": 10.0, "entry_price": 100.0, "init_qty": 10.0,
                               "stop_price": 96.0, "take_profit_price": 104.0,
                               "stop_type": "synthetic", "scaled": [],
                               "entry_ts": "2026-06-05T13:00:00+00:00"}
state_file.write_text(_json.dumps(istate))
ictx = ctx(positions=[{"symbol": "AAPL", "last": 95.0}])
ictx["market_open"], ictx["data_stale"] = True, False
ictx["screen"]["exits"] = [{"symbol": "AAPL", "reason": "synthetic stop hit: 95 <= stop 96 (-5%)"}]
sen.tc.build_context = lambda now=None: ictx          # inject the synthetic view
sen.STATE_PATH = state_file
sen.ENGINE_LOG = log_file
sen.trade_log.record_fills = lambda *a, **k: []        # don't touch the real blotter
rc = sen.main()
out = _json.loads(state_file.read_text())
logged = [_json.loads(line) for line in log_file.read_text().splitlines()] if log_file.exists() else []
check("main() returns 0", rc == 0)
check("sentinel sold the stopped-out position", "AAPL" not in out["positions"])
check("engine-log got a source='sentinel' fill record",
      any(r.get("source") == "sentinel" and r.get("n_filled", 0) >= 1 for r in logged))

print("\nLIVE PATH (decision file -> apply_decision arms -> sentinel fires on the cross)")
import sys as _sys
tmp2 = Path(tempfile.mkdtemp())
sfile, ctxfile, decfile, elog = (tmp2 / "paper_state.json", tmp2 / "ctx.json",
                                 tmp2 / "dec.json", tmp2 / "engine.jsonl")
sfile.write_text(_json.dumps(fresh_state(cash=1000.0)))
ictx2 = ctx(candidates=[{"symbol": "AAPL", "last": 300.0}])          # below the 315 trigger
ctxfile.write_text(_json.dumps(ictx2))
# exactly the action decide.py emits for an armed DD commit:
decfile.write_text(_json.dumps({"actions": [{"symbol": "AAPL", "side": "buy", "dollar_amount": 100,
    "arm": True, "entry_trigger": {"price": 315.0, "direction": "above"}, "conviction": "high",
    "reason": "[DD/high] breakout-armed"}], "rationale": "t"}))
ad.STATE_PATH, ad.ENGINE_LOG = sfile, elog
ad.trade_log.record_fills = lambda *a, **k: []
_argv = _sys.argv
_sys.argv = ["apply_decision", "--context", str(ctxfile), "--decision", str(decfile)]
ad.main()
_sys.argv = _argv
out2 = _json.loads(sfile.read_text())
check("apply_decision ARMED the entry (no immediate buy)",
      "AAPL" in out2.get("armed_entries", {}) and "AAPL" not in out2["positions"])
# sentinel fires it once price crosses the trigger
sen.STATE_PATH, sen.ENGINE_LOG = sfile, elog
fctx = ctx(candidates=[{"symbol": "AAPL", "last": 316.0}])           # now above the trigger
sen.tc.build_context = lambda now=None: fctx
rc3 = sen.main()
out3 = _json.loads(sfile.read_text())
check("sentinel FIRED the armed entry on the cross",
      "AAPL" in out3["positions"] and "AAPL" not in out3.get("armed_entries", {}))
check("sentinel.main returned 0", rc3 == 0)

print(f"\n{'='*48}\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
