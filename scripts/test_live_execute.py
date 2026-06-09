#!/usr/bin/env python3
"""
test_live_execute.py — dependency-free unit tests for the LIVE executor's PURE logic (order-spec
builders, cap re-checks, snapshot parsing, review-alert gating). These are the parts that decide
real-money orders, so they're tested without touching the MCP or placing anything.

Run:  python3 scripts/test_live_execute.py     (exits non-zero on first failure)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("PREFER_WHOLE_SHARES", "1")

import live_execute as le  # noqa: E402

CAPS = {"MAX_POSITION_USD": 300, "MAX_TOTAL_EXPOSURE_USD": 2400, "MAX_OPEN_POSITIONS": 10,
        "STOP_LOSS_PCT": 4.0, "TAKE_PROFIT_PCT": 12.0, "MAX_PER_TRADE_LOSS_USD": 300,
        "MIN_POSITION_USD": 0, "MARKETABLE_LIMIT_PCT": 0.5, "DAILY_MAX_LOSS_USD": 150}

_passed = 0


def check(name: str, cond: bool, extra: object = "") -> None:
    global _passed
    if not cond:
        print(f"FAIL: {name} {extra}")
        raise SystemExit(1)
    _passed += 1


def test_whole_share_entry():
    p = le.size_entry(280, {"ask": 50.0, "last": 49.9}, CAPS)
    check("whole kind", p["kind"] == "limit")
    check("whole floored to 5", p["qty"] == 5.0, p)
    check("whole resting stop", p["stop_type"] == "resting")
    check("whole limit price = ask*1.005", p["limit_price"] == 50.25, p)
    spec = le.buy_spec("ABC", p)
    check("buy_spec is limit", spec["type"] == "limit" and spec["quantity"] == "5")
    check("buy_spec strings", spec["limit_price"] == "50.25" and spec["time_in_force"] == "gfd")
    stop = le.stop_spec("ABC", p["qty"], 48.05)
    check("stop is gtc stop_market", stop["type"] == "stop_market" and stop["time_in_force"] == "gtc")
    check("stop qty whole-string", stop["quantity"] == "5" and stop["stop_price"] == "48.05")


def test_rounds_up_to_one_share():
    # Whole-share-only: a budget short of 1 share rounds UP to exactly 1 when 1 share fits the
    # position cap (ref $250 <= MAX_POSITION_USD $300) -> still a limit + real resting stop, never
    # a fractional market order.
    p = le.size_entry(100, {"ask": 250.0, "last": 249.0}, CAPS)
    check("rounds up to 1 share", p["qty"] == 1.0, p)
    check("still a limit", p["kind"] == "limit", p)
    check("still a resting stop", p["stop_type"] == "resting", p)
    check("limit price = ask*1.005", p["limit_price"] == 251.25, p)
    check("notional = 1 * limit", p["notional"] == 251.25, p)
    spec = le.buy_spec("HI", p)
    check("buy_spec is limit (not market)", spec["type"] == "limit" and spec["quantity"] == "1", spec)
    check("buy_spec has a limit price", spec.get("limit_price") == "251.25", spec)


def test_one_share_over_cap_rejects():
    # If even 1 share exceeds MAX_POSITION_USD, the entry is SKIPPED (ok=False) rather than degraded
    # to fractional — replaces the old canary-notional path.
    p = le.size_entry(100, {"ask": 500.0}, CAPS)
    check("1 share over cap -> not ok", p.get("ok") is False, p)
    check("reject reason explains cap", "cap" in (p.get("reject_reason") or "").lower(), p)
    check("no notional on a reject", "notional" not in p, p)


def test_cap_rejects():
    p = le.size_entry(280, {"ask": 50.0}, CAPS)
    ok, why = le.check_entry_caps(p, existing_val=0, exposure=2300, buying_power=1000,
                                  n_positions=2, held=False, caps=CAPS, day_pnl=0)
    check("exposure cap rejects", not ok and "EXPOSURE" in why.upper(), why)
    ok, why = le.check_entry_caps(p, existing_val=0, exposure=0, buying_power=100,
                                  n_positions=2, held=False, caps=CAPS, day_pnl=0)
    check("buying power rejects", not ok and "buying power" in why, why)
    ok, why = le.check_entry_caps(p, existing_val=0, exposure=0, buying_power=1000,
                                  n_positions=2, held=False, caps=CAPS, day_pnl=-200)
    check("breaker rejects", not ok and "circuit_breaker" in why, why)
    ok, why = le.check_entry_caps(p, existing_val=0, exposure=0, buying_power=1000,
                                  n_positions=10, held=False, caps=CAPS, day_pnl=0)
    check("max positions rejects new name", not ok and "MAX_OPEN_POSITIONS" in why, why)
    ok, why = le.check_entry_caps(p, existing_val=0, exposure=0, buying_power=1000,
                                  n_positions=2, held=False, caps=CAPS, day_pnl=0)
    check("clean passes", ok, why)


def test_sell_specs():
    s = le.sell_spec("ABC", 5, whole=True, quote={"bid": 48.0, "last": 48.1}, caps=CAPS)
    check("whole sell = marketable limit below bid", s["type"] == "limit" and s["limit_price"] == "47.76", s)
    f = le.sell_spec("HI", 0.4, whole=False, quote={"bid": 250}, caps=CAPS)
    check("frac sell = market", f["type"] == "market" and f["quantity"] == "0.400000", f)
    # urgent (protective full-close) = MARKET even for a whole-share lot, so it can't strand as a
    # resting non-marketable limit while the lot sits naked.
    u = le.sell_spec("ABC", 5, whole=True, quote={"bid": 48.0, "last": 48.1}, caps=CAPS, urgent=True)
    check("urgent whole sell = market", u["type"] == "market" and u["quantity"] == "5", u)


def test_review_gating():
    # real shape: review wrapped in data; order_checks {} = clear
    clear = {"review": {"data": {"symbol": "F", "order_checks": {}}}}
    check("empty order_checks -> clear", le.review_blocking(clear) == [])
    # real clean-order alert that must NOT block (routine individual-account disclosure)
    suit = {"review": {"data": {"order_checks": {"alertType": "EQUITY_SUITABILITY",
            "equitySuitabilityAlertDetails": {"brokerageAccountType": "INDIVIDUAL"}}}}}
    check("EQUITY_SUITABILITY passes", le.review_blocking(suit) == [], le.review_blocking(suit))
    # genuinely-blocking conditions
    bp = {"review": {"data": {"order_checks": {"alertType": "INSUFFICIENT_BUYING_POWER"}}}}
    check("buying power blocks", le.review_blocking(bp) == ["INSUFFICIENT_BUYING_POWER"], le.review_blocking(bp))
    pdt = {"review": {"data": {"order_checks": {"alertType": "PATTERN_DAY_TRADE_PROTECTION"}}}}
    check("PDT blocks", le.review_blocking(pdt) == ["PATTERN_DAY_TRADE_PROTECTION"])
    check("unparseable blocks (fail-safe)", le.review_blocking(None) == ["review_unparseable"])
    check("relay error blocks", le.review_blocking({"errors": {"step1": "timeout"}, "review": None}) != [])


def test_snapshot_parse_real_shapes():
    # EXACT shapes captured from live MCP 2026-06-04: every tool wraps results in {"data": ...};
    # buying_power is nested; quotes are data.results[].quote; a stop reads via stop_price.
    snap = {
        "portfolio": {"data": {"cash": "1064", "buying_power": {"buying_power": "1064.0000",
                      "unleveraged_buying_power": "1064.0000", "display_currency": "USD"}}},
        "positions": {"data": {"positions": [
            {"symbol": "ABC", "quantity": "5.000000", "average_buy_price": "50.05",
             "shares_available_for_sells": "5.000000"},
            {"symbol": "HI", "quantity": "0.400000", "average_buy_price": "250"}]}},
        "quotes": {"data": {"results": [
            {"quote": {"symbol": "ABC", "bid_price": "48.00", "ask_price": "48.10",
                       "last_trade_price": "48.05"}, "close": {"symbol": "ABC", "price": "47.5"}}]}},
        "orders": {"data": {"orders": [
            {"id": "o1", "symbol": "ABC", "side": "sell", "type": "market", "trigger": "stop",
             "stop_price": "48.00", "state": "confirmed"}]}},
    }
    b = le.parse_snapshot(snap)
    check("nested buying_power parsed", b["buying_power"] == 1064.0, b["buying_power"])
    check("data.positions parsed", b["positions"]["ABC"]["qty"] == 5.0 and b["positions"]["ABC"]["avg_cost"] == 50.05)
    check("keeps fractional position", "HI" in b["positions"])
    check("data.results[].quote parsed", b["quotes"]["ABC"]["ask"] == 48.10 and b["quotes"]["ABC"]["bid"] == 48.0)
    check("stop detected via stop_price (type=market+trigger=stop)", le.open_stop_for(b["orders"], "ABC") is not None)
    check("no stop for unknown sym", le.open_stop_for(b["orders"], "ZZZ") is None)


def test_buying_power_fallback_to_cash():
    # if buying_power is absent, fall back to cash (also data-wrapped)
    b = le.parse_snapshot({"portfolio": {"data": {"cash": "500"}}, "positions": {"data": {"positions": []}}})
    check("cash fallback", b["buying_power"] == 500.0, b)


def test_order_obj_and_place_failure():
    # success: relay echoes {"order": {"data": {...}}, "errors": {}}
    ok = le.order_obj({"order": {"data": {"id": "abc-123", "state": "confirmed"}}, "errors": {}})
    check("order_obj extracts the order", isinstance(ok, dict) and ok.get("id") == "abc-123", str(ok))
    # the real failure mode this live run hit: place 400s (incomplete investor profile) -> no order
    fail = {"order": None, "errors": {"step1": "API error 400: investor profile required"}}
    check("order_obj None on failed place", le.order_obj(fail) is None)
    check("relay junk -> None", le.order_obj("not a dict") is None)


def test_reconcile_books_close():
    # a lot the broker no longer holds (and that wasn't a pending unfilled entry) is a real closure:
    # it's dropped, booked as closed_external, and stamped with a re-entry cooldown.
    state = {"lots": {"ABC": {"qty": 5, "entry_price": 50.0, "adopted": False,
                              "resting_stop_order_id": "s1"}},
             "_caps": {"STOP_LOSS_PCT": 4.0, "TAKE_PROFIT_PCT": 12.0}}
    le.reconcile(state, {"positions": {}, "orders": [], "quotes": {}}, log := [])
    check("closed lot dropped", "ABC" not in state["lots"])
    check("close booked as closed_external", any(e["event"] == "closed_external" for e in log))
    check("last_exit recorded", "ABC" in (state.get("last_exit") or {}))


def test_reconcile_adopted_close_also_booked():
    # With the canary gone, an ADOPTED position closing is booked exactly like one we entered — the
    # adopted/self distinction only ever existed to gate the (now-removed) canary round-trip.
    state = {"lots": {"XYZ": {"qty": 3, "entry_price": 10.0, "adopted": True}}, "_caps": {}}
    le.reconcile(state, {"positions": {}, "orders": [], "quotes": {}}, log := [])
    check("adopted close dropped", "XYZ" not in state["lots"])
    check("adopted close booked as closed_external", any(e["event"] == "closed_external" for e in log))
    check("adopted close stamps cooldown", "XYZ" in (state.get("last_exit") or {}))


def test_reconcile_pending_not_booked_closed():
    # a PENDING entry from a prior tick that never filled must NOT be booked as a closed position,
    # must not count as a round-trip, and must not leave a cooldown stamp. (Unarmed -> no cancel.)
    state = {"lots": {"PEND": {"pending": True, "entry_order_id": "o9"}}, "_caps": {}}
    le.reconcile(state, {"positions": {}, "orders": [], "quotes": {}}, log := [])
    check("pending lot dropped", "PEND" not in state["lots"])
    check("pending NOT booked as closed", not any(e["event"] == "closed_external" for e in log))
    check("pending logged unfilled", any(e["event"] == "entry_unfilled" for e in log))
    check("no cooldown for unfilled entry", "PEND" not in (state.get("last_exit") or {}))


def test_trail_off_by_default():
    caps = {"STOP_LOSS_PCT": 8.0}  # TRAIL_STOP_PCT absent -> off
    lot = {"entry_price": 100.0, "stop_price": 92.0, "high_water": 100.0}
    ns, hw = le.trail_stop_price(lot, caps, 200.0)
    check("trail off -> no stop change", ns is None, (ns, hw))
    check("trail off still tracks high-water", hw == 200.0, hw)


def test_trail_ratchets_up_and_never_down():
    caps = {"STOP_LOSS_PCT": 8.0, "TRAIL_STOP_PCT": 5.0, "TRAIL_ACTIVATE_PCT": 4.0, "TRAIL_MIN_STEP_PCT": 0.5}
    lot = {"entry_price": 100.0, "stop_price": 92.0, "high_water": 100.0}
    # +3% (last 103) < 4% activate -> fixed stop stands
    ns, hw = le.trail_stop_price(lot, caps, 103.0)
    check("trail not yet activated", ns is None and hw == 103.0, (ns, hw))
    # +10% (last 110) -> activate; stop -> 110*0.95 = 104.5
    lot["high_water"] = hw
    ns, hw = le.trail_stop_price(lot, caps, 110.0)
    check("trail activates and raises stop", ns == 104.5 and hw == 110.0, (ns, hw))
    # pullback to 106 -> high-water holds at 110, no lowering
    lot["stop_price"], lot["high_water"] = ns, hw
    ns, hw = le.trail_stop_price(lot, caps, 106.0)
    check("trail never lowers on pullback", ns is None and hw == 110.0, (ns, hw))


def test_trail_min_step_guard():
    caps = {"STOP_LOSS_PCT": 8.0, "TRAIL_STOP_PCT": 5.0, "TRAIL_ACTIVATE_PCT": 0.0, "TRAIL_MIN_STEP_PCT": 1.0}
    lot = {"entry_price": 100.0, "stop_price": 104.5, "high_water": 110.0}
    # 110.2 -> desired 104.69, only +0.18% over 104.5 (< 1% step) -> suppressed
    ns, _ = le.trail_stop_price(lot, caps, 110.2)
    check("min-step suppresses tiny ratchet", ns is None, ns)
    # 115 -> desired 109.25, well over the step -> allowed
    ns, _ = le.trail_stop_price(lot, caps, 115.0)
    check("min-step allows a real ratchet", ns == 109.25, ns)


def test_trail_floored_at_initial_stop():
    # an aggressive trail (15%) on a barely-activated lot must never set the stop BELOW the entry floor
    caps = {"STOP_LOSS_PCT": 8.0, "TRAIL_STOP_PCT": 15.0, "TRAIL_ACTIVATE_PCT": 0.0, "TRAIL_MIN_STEP_PCT": 0.0}
    lot = {"entry_price": 100.0, "stop_price": 92.0, "high_water": 100.0}
    ns, _ = le.trail_stop_price(lot, caps, 100.0)  # 100*0.85=85 < floor 92 -> stays 92 -> no raise
    check("trail floored at initial stop", ns is None, ns)


def test_breakeven_rung_lifts_to_entry():
    # breakeven rung alone (trail off): once up TRAIL_BREAKEVEN_AT_PCT, stop -> entry, then holds.
    caps = {"STOP_LOSS_PCT": 8.0, "TRAIL_BREAKEVEN_AT_PCT": 10.0, "TRAIL_MIN_STEP_PCT": 0.0}
    lot = {"entry_price": 100.0, "stop_price": 92.0, "high_water": 100.0}
    ns, _ = le.trail_stop_price(lot, caps, 108.0)            # +8% < 10% -> not yet
    check("breakeven not engaged below trigger", ns is None, ns)
    ns, hw = le.trail_stop_price(lot, caps, 111.0)           # +11% -> lift to entry 100
    check("breakeven lifts stop to entry", ns == 100.0 and hw == 111.0, (ns, hw))
    lot["stop_price"], lot["high_water"] = ns, hw
    ns, _ = le.trail_stop_price(lot, caps, 105.0)            # pullback: holds at breakeven
    check("breakeven holds on pullback", ns is None, ns)


def test_breakeven_and_trail_compose():
    # both rungs on: breakeven floors at entry up to the trail's activation, then the trail takes over.
    caps = {"STOP_LOSS_PCT": 8.0, "TRAIL_BREAKEVEN_AT_PCT": 10.0,
            "TRAIL_STOP_PCT": 12.0, "TRAIL_ACTIVATE_PCT": 15.0, "TRAIL_MIN_STEP_PCT": 0.0}
    lot = {"entry_price": 100.0, "stop_price": 92.0, "high_water": 100.0}
    ns, _ = le.trail_stop_price(lot, caps, 112.0)            # +12%: breakeven yes, trail not active (<15)
    check("compose uses breakeven below trail activation", ns == 100.0, ns)
    lot["stop_price"] = ns
    ns, _ = le.trail_stop_price(lot, caps, 120.0)            # +20%: trail 120*0.88=105.6 > entry
    check("compose trail overtakes breakeven above activation", ns == 105.6, ns)


def test_next_settle_date_skips_weekends():
    os.environ["CASH_SETTLEMENT_GUARD"] = "1"
    check("Mon -> Tue (T+1)", le.next_settle_date("2026-06-08") == "2026-06-09", le.next_settle_date("2026-06-08"))
    check("Fri -> Mon (skip weekend)", le.next_settle_date("2026-06-05") == "2026-06-08", le.next_settle_date("2026-06-05"))


def test_settled_buying_power_excludes_unsettled_and_pending():
    os.environ["CASH_SETTLEMENT_GUARD"] = "1"
    broker = {"buying_power": 2064.0, "pending_deposits": 1000.0}
    # an unsettled $300 sale that settles tomorrow + the $1000 pending deposit are both excluded
    state = {"unsettled": [{"settle_date": "2026-06-09", "amount": 300.0, "symbol": "ABC"}]}
    sbp, uns = le.settled_buying_power(state, broker, "2026-06-08")
    check("settled = bp - unsettled - pending", sbp == 2064.0 - 300.0 - 1000.0, sbp)
    check("unsettled total reported", uns == 300.0, uns)
    # a matured sale (settle_date <= today) is pruned and counts as settled again
    state2 = {"unsettled": [{"settle_date": "2026-06-08", "amount": 300.0, "symbol": "ABC"}]}
    sbp2, uns2 = le.settled_buying_power(state2, broker, "2026-06-08")
    check("matured sale pruned", uns2 == 0.0 and not state2["unsettled"], (uns2, state2["unsettled"]))
    check("settled excludes only pending after prune", sbp2 == 2064.0 - 1000.0, sbp2)


def test_settled_guard_off_returns_raw_bp():
    os.environ["CASH_SETTLEMENT_GUARD"] = "0"
    broker = {"buying_power": 2064.0, "pending_deposits": 1000.0}
    state = {"unsettled": [{"settle_date": "2099-01-01", "amount": 500.0}]}
    sbp, uns = le.settled_buying_power(state, broker, "2026-06-08")
    check("guard off -> raw bp, nothing excluded", sbp == 2064.0 and uns == 0.0, (sbp, uns))
    os.environ["CASH_SETTLEMENT_GUARD"] = "1"


def test_reconcile_trails_resting_stop():
    import types
    calls = {"cancel": [], "place": []}
    fake = types.ModuleType("rh_mcp")
    fake.cancel = lambda oid: (calls["cancel"].append(oid), {"cancel": "ok", "errors": {}})[1]
    fake.place = lambda spec, ref_id: (calls["place"].append(spec),
                                       {"order": {"data": {"id": "s2"}}, "errors": {}})[1]
    saved = sys.modules.get("rh_mcp")
    sys.modules["rh_mcp"] = fake
    os.environ["LIVE_ARMED"] = "1"
    try:
        state = {"lots": {"ABC": {"qty": 5, "entry_price": 100.0, "stop_price": 92.0,
                                  "high_water": 100.0, "resting_stop_order_id": "s1",
                                  "stop_type": "resting", "adopted": False}},
                 "_caps": {"STOP_LOSS_PCT": 8.0, "TAKE_PROFIT_PCT": 25.0, "TRAIL_STOP_PCT": 5.0,
                           "TRAIL_ACTIVATE_PCT": 4.0, "TRAIL_MIN_STEP_PCT": 0.5}}
        broker = {"positions": {"ABC": {"qty": 5.0, "avg_cost": 100.0}},
                  "orders": [{"id": "s1", "symbol": "ABC", "side": "sell",
                              "stop_price": "92.00", "state": "confirmed"}],
                  "quotes": {"ABC": {"last": 110.0, "bid": 109.9, "ask": 110.1}}}
        le.reconcile(state, broker, log := [])
        lot = state["lots"]["ABC"]
        check("trail cancelled the old stop", "s1" in calls["cancel"], calls)
        check("trail placed a new stop @104.50", len(calls["place"]) == 1
              and calls["place"][0]["stop_price"] == "104.50", calls)
        check("trail stored new resting id", lot["resting_stop_order_id"] == "s2", lot)
        check("trail raised stop_price", lot["stop_price"] == 104.5, lot)
        check("trail tracked high-water", lot["high_water"] == 110.0, lot)
        check("trail logged a rearm", any(e["event"] == "trail_rearm" for e in log), log)
    finally:
        os.environ.pop("LIVE_ARMED", None)
        if saved is not None:
            sys.modules["rh_mcp"] = saved
        else:
            sys.modules.pop("rh_mcp", None)


def test_reconcile_trail_dryrun_places_nothing():
    import types
    calls = {"cancel": [], "place": []}
    fake = types.ModuleType("rh_mcp")
    fake.cancel = lambda oid: (calls["cancel"].append(oid), {"errors": {}})[1]
    fake.place = lambda spec, ref_id: (calls["place"].append(spec), {"order": None, "errors": {}})[1]
    saved = sys.modules.get("rh_mcp")
    sys.modules["rh_mcp"] = fake
    os.environ.pop("LIVE_ARMED", None)  # dry-run
    try:
        state = {"lots": {"ABC": {"qty": 5, "entry_price": 100.0, "stop_price": 92.0,
                                  "high_water": 100.0, "resting_stop_order_id": "s1",
                                  "stop_type": "resting", "adopted": False}},
                 "_caps": {"STOP_LOSS_PCT": 8.0, "TAKE_PROFIT_PCT": 25.0, "TRAIL_STOP_PCT": 5.0,
                           "TRAIL_ACTIVATE_PCT": 4.0, "TRAIL_MIN_STEP_PCT": 0.5}}
        broker = {"positions": {"ABC": {"qty": 5.0, "avg_cost": 100.0}},
                  "orders": [{"id": "s1", "symbol": "ABC", "side": "sell",
                              "stop_price": "92.00", "state": "confirmed"}],
                  "quotes": {"ABC": {"last": 110.0}}}
        le.reconcile(state, broker, log := [])
        check("dry-run places nothing", calls["place"] == [] and calls["cancel"] == [], calls)
        check("dry-run reflects intended stop", state["lots"]["ABC"]["stop_price"] == 104.5, state["lots"]["ABC"])
        check("dry-run logged rearm_dryrun", any(e["event"] == "trail_rearm_dryrun" for e in log), log)
    finally:
        if saved is not None:
            sys.modules["rh_mcp"] = saved
        else:
            sys.modules.pop("rh_mcp", None)


def test_run_relays_parallel_overlaps_and_isolates():
    import threading
    import time as _t
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def job(sym, fail=False):
        def thunk():
            with lock:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            try:
                _t.sleep(0.2)
                if fail:
                    raise RuntimeError("relay boom")
                return {"symbol": sym, "side": "sell", "status": "placed"}
            finally:
                with lock:
                    active["now"] -= 1
        return (sym, thunk)

    os.environ.pop("LIVE_PARALLEL_ORDERS", None)
    os.environ.pop("LIVE_PARALLEL_ENTRIES", None)
    # parallel (default on): all results collected, and they genuinely overlap
    res = le._run_relays_parallel([job(s) for s in ("A", "B", "C", "D")], side="sell")
    check("parallel collects every result", len(res) == 4, res)
    check("relays actually overlapped", active["max"] >= 2, active)
    # a raising thunk becomes a 'failed' result without sinking the batch
    active.update(now=0, max=0)
    res = le._run_relays_parallel([job("A"), job("B", fail=True), job("C")], side="sell")
    byb = {r["symbol"]: r["status"] for r in res}
    check("one failure isolated, others placed",
          byb.get("B") == "failed" and byb.get("A") == "placed" and byb.get("C") == "placed", byb)
    # opt-out serializes (max concurrency 1); the OLD knob name still works as the fallback
    for knob in ("LIVE_PARALLEL_ORDERS", "LIVE_PARALLEL_ENTRIES"):
        os.environ[knob] = "0"
        active.update(now=0, max=0)
        le._run_relays_parallel([job(s) for s in ("A", "B", "C")], side="sell")
        check(f"{knob}=0 serializes", active["max"] == 1, active)
        del os.environ[knob]


def test_execute_sell_full_close_is_market():
    """A full-close exit (no qty) on a whole-share lot cancels the resting stop AND places a MARKET
    sell — the fix for the stranded risk-exit limit (ALOY)."""
    import types
    calls = {"cancel": [], "place": []}
    fake = types.ModuleType("rh_mcp")
    fake.cancel = lambda oid: (calls["cancel"].append(oid), {"errors": {}})[1]
    fake.place = lambda spec, ref_id: (calls["place"].append(spec),
                                       {"order": {"data": {"id": "x1"}}, "errors": {}})[1]
    saved = sys.modules.get("rh_mcp")
    sys.modules["rh_mcp"] = fake
    os.environ["LIVE_ARMED"] = "1"
    try:
        state = {"lots": {"ALOY": {"qty": 1, "entry_price": 14.08, "stop_price": 12.39,
                                   "resting_stop_order_id": "stop1", "stop_type": "resting"}}}
        broker = {"positions": {"ALOY": {"qty": 1.0, "avg_cost": 14.08}},
                  "quotes": {"ALOY": {"bid": 12.74, "last": 12.76, "ask": 12.78}}}
        res = le.execute_sell("ALOY", {"reason": "risk-exit: CRITICAL"}, state, broker, CAPS, log := [])
        check("full close cancelled the resting stop", "stop1" in calls["cancel"], calls)
        check("full close placed a MARKET sell", len(calls["place"]) == 1
              and calls["place"][0]["type"] == "market", calls)
        check("full close marked urgent", res.get("urgent") is True, res)
        check("lot stop id cleared after sell", state["lots"]["ALOY"]["resting_stop_order_id"] is None, state)
    finally:
        os.environ.pop("LIVE_ARMED", None)
        if saved is not None:
            sys.modules["rh_mcp"] = saved
        else:
            sys.modules.pop("rh_mcp", None)


def test_execute_sell_rearms_stop_on_failed_sell():
    """If the sell place fails after we've cancelled the stop, re-arm it — never leave the lot naked."""
    import types
    calls = {"cancel": [], "place": []}
    fake = types.ModuleType("rh_mcp")
    fake.cancel = lambda oid: (calls["cancel"].append(oid), {"errors": {}})[1]

    def _place(spec, ref_id):
        calls["place"].append(spec)
        if spec["side"] == "sell" and spec["type"] == "market":   # the exit fails
            return {"order": None, "errors": {"detail": "rejected"}}
        return {"order": {"data": {"id": "newstop"}}, "errors": {}}  # the re-arm succeeds
    fake.place = _place
    saved = sys.modules.get("rh_mcp")
    sys.modules["rh_mcp"] = fake
    os.environ["LIVE_ARMED"] = "1"
    try:
        state = {"lots": {"ALOY": {"qty": 1, "entry_price": 14.08, "stop_price": 12.39,
                                   "resting_stop_order_id": "stop1", "stop_type": "resting"}}}
        broker = {"positions": {"ALOY": {"qty": 1.0, "avg_cost": 14.08}},
                  "quotes": {"ALOY": {"bid": 12.74, "last": 12.76, "ask": 12.78}}}
        res = le.execute_sell("ALOY", {"reason": "risk-exit"}, state, broker, CAPS, log := [])
        lot = state["lots"]["ALOY"]
        check("sell reported failed", res["status"] == "failed", res)
        check("a re-arm stop_market was placed", any(s["type"] == "stop_market" for s in calls["place"]), calls)
        check("lot re-armed with the new stop id", lot["resting_stop_order_id"] == "newstop", lot)
        check("lot is resting again, not naked", lot["stop_type"] == "resting", lot)
        check("re-arm was logged", any(e["event"] == "stop_rearmed_after_failed_sell" for e in log), log)
    finally:
        os.environ.pop("LIVE_ARMED", None)
        if saved is not None:
            sys.modules["rh_mcp"] = saved
        else:
            sys.modules.pop("rh_mcp", None)


def test_reconcile_cancels_stranded_sell_then_arms():
    """A whole-share lot with no resting stop but a stranded open sell limit: reconcile cancels the
    stranded order, then arms a fresh resting stop (auto-recovery for a lot left naked)."""
    import types
    calls = {"cancel": [], "place": []}
    fake = types.ModuleType("rh_mcp")
    fake.cancel = lambda oid: (calls["cancel"].append(oid), {"errors": {}})[1]
    fake.place = lambda spec, ref_id: (calls["place"].append(spec),
                                       {"order": {"data": {"id": "freshstop"}}, "errors": {}})[1]
    saved = sys.modules.get("rh_mcp")
    sys.modules["rh_mcp"] = fake
    os.environ["LIVE_ARMED"] = "1"
    try:
        state = {"lots": {"ALOY": {"qty": 1, "entry_price": 14.08, "stop_price": 12.39,
                                   "resting_stop_order_id": None, "stop_type": "resting"}},
                 "_caps": {"STOP_LOSS_PCT": 12.0, "TAKE_PROFIT_PCT": 40.0}}
        broker = {"positions": {"ALOY": {"qty": 1.0, "avg_cost": 14.08}},
                  # a stranded exit limit (no stop_price) holding the share — and NO resting stop
                  "orders": [{"id": "stuck", "symbol": "ALOY", "side": "sell", "type": "limit",
                              "price": "12.95", "state": "confirmed"}],
                  "quotes": {"ALOY": {"last": 12.76, "bid": 12.74, "ask": 12.78}}}
        le.reconcile(state, broker, log := [])
        lot = state["lots"]["ALOY"]
        check("stranded sell limit was cancelled", "stuck" in calls["cancel"], calls)
        check("a fresh resting stop was armed", any(s["type"] == "stop_market" for s in calls["place"]), calls)
        check("lot now holds the fresh stop id", lot["resting_stop_order_id"] == "freshstop", lot)
        check("stranded-cancel was logged", any(e["event"] == "stranded_sell_cancelled" for e in log), log)
    finally:
        os.environ.pop("LIVE_ARMED", None)
        if saved is not None:
            sys.modules["rh_mcp"] = saved
        else:
            sys.modules.pop("rh_mcp", None)


if __name__ == "__main__":
    tests = [test_whole_share_entry, test_rounds_up_to_one_share, test_one_share_over_cap_rejects,
             test_cap_rejects, test_sell_specs, test_review_gating, test_snapshot_parse_real_shapes,
             test_buying_power_fallback_to_cash, test_order_obj_and_place_failure,
             test_reconcile_books_close, test_reconcile_adopted_close_also_booked,
             test_reconcile_pending_not_booked_closed,
             test_trail_off_by_default, test_trail_ratchets_up_and_never_down,
             test_trail_min_step_guard, test_trail_floored_at_initial_stop,
             test_breakeven_rung_lifts_to_entry, test_breakeven_and_trail_compose,
             test_next_settle_date_skips_weekends, test_settled_buying_power_excludes_unsettled_and_pending,
             test_settled_guard_off_returns_raw_bp,
             test_reconcile_trails_resting_stop, test_reconcile_trail_dryrun_places_nothing,
             test_run_relays_parallel_overlaps_and_isolates,
             test_execute_sell_full_close_is_market, test_execute_sell_rearms_stop_on_failed_sell,
             test_reconcile_cancels_stranded_sell_then_arms]
    for fn in tests:
        fn()
    print(f"OK — {_passed} assertions passed across {len(tests)} tests")
