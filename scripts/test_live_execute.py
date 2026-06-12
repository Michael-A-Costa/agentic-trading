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


def test_reconcile_losing_exit_stamps_cooldown():
    # OWNER RULE 2026-06-12: a LOSING exit (stop resting BELOW entry = the catastrophe floor) cools
    # the name down as before.
    state = {"lots": {"LOSS": {"qty": 4, "entry_price": 50.0, "stop_price": 44.0,
                               "resting_stop_order_id": "s1"}}, "_caps": {}}
    le.reconcile(state, {"positions": {}, "orders": [], "quotes": {}}, log := [])
    check("losing exit booked closed", any(e["event"] == "closed_external" for e in log))
    check("losing exit stamps cooldown", "LOSS" in (state.get("last_exit") or {}))


def test_reconcile_breakeven_exit_skips_cooldown():
    # OWNER RULE 2026-06-12: a BREAKEVEN-or-better exit (stop resting AT/ABOVE entry — be5 lifted it,
    # or a green trail/TP) is not a whipsaw loss, so it does NOT cool the name down — the stock is
    # instantly re-buyable if it turns back up. Lot still booked as a real closure.
    state = {"lots": {"FLAT": {"qty": 4, "entry_price": 50.0, "stop_price": 50.0,
                               "resting_stop_order_id": "s1"},
                      "GREEN": {"qty": 4, "entry_price": 50.0, "stop_price": 53.0,
                                "resting_stop_order_id": "s2"}}, "_caps": {}}
    le.reconcile(state, {"positions": {}, "orders": [], "quotes": {}}, log := [])
    check("breakeven exit still booked closed", sum(e["event"] == "closed_external" for e in log) == 2)
    check("breakeven (stop==entry) skips cooldown", "FLAT" not in (state.get("last_exit") or {}))
    check("green (stop>entry) skips cooldown", "GREEN" not in (state.get("last_exit") or {}))


def test_reconcile_adoption_distinct_costs():
    """Adopting two never-tracked broker positions derives each lot's stop/TP from ITS OWN avg
    cost. A copy/aliasing bug in the adoption path would hand adopted lots identical risk levels —
    the F/SRAD identical-state scare from the 2026-06-09 audit (both genuinely filled at $15.17)
    is exactly what that bug would look like, so this pins the invariant (remediation plan P7)."""
    state = {"lots": {}, "_caps": {"STOP_LOSS_PCT": 12.0, "TAKE_PROFIT_PCT": 40.0}}
    broker = {"positions": {"AAA": {"qty": 1.0, "avg_cost": 20.0, "sellable": 1.0},
                            "BBB": {"qty": 1.0, "avg_cost": 80.0, "sellable": 1.0}},
              "orders": [], "quotes": {}}
    le.reconcile(state, broker, log := [])
    a, b = state["lots"]["AAA"], state["lots"]["BBB"]
    check("both adopted", a.get("adopted") is True and b.get("adopted") is True, (a, b))
    check("lot dicts are distinct objects", a is not b)
    check("entry from OWN avg cost", a["entry_price"] == 20.0 and b["entry_price"] == 80.0, (a, b))
    check("stops derived per-lot (12%)", a["stop_price"] == 17.6 and b["stop_price"] == 70.4, (a, b))
    check("tps derived per-lot (40%)", a["take_profit_price"] == 28.0
          and b["take_profit_price"] == 112.0, (a, b))


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


def test_breakeven_offset_lifts_above_entry():
    # OWNER RULE 2026-06-12: TRAIL_BREAKEVEN_OFFSET_PCT lifts the rung to entry x (1+off%) — a TRUE
    # no-loss floor (+1% covers round-trip cost + locks a small gain), not entry exactly.
    caps = {"STOP_LOSS_PCT": 12.0, "TRAIL_BREAKEVEN_AT_PCT": 5.0,
            "TRAIL_BREAKEVEN_OFFSET_PCT": 1.0, "TRAIL_MIN_STEP_PCT": 0.0}
    lot = {"entry_price": 100.0, "stop_price": 88.0, "high_water": 100.0}
    ns, _ = le.trail_stop_price(lot, caps, 104.0)            # +4% < 5% -> not yet
    check("offset rung not engaged below trigger", ns is None, ns)
    ns, hw = le.trail_stop_price(lot, caps, 106.0)           # +6% -> lift to entry*1.01 = 101.0
    check("offset lifts stop to entry+1%", ns == 101.0 and hw == 106.0, (ns, hw))
    # offset=0 keeps the literal-breakeven behavior (regression guard for the default).
    caps0 = {**caps, "TRAIL_BREAKEVEN_OFFSET_PCT": 0.0}
    lot0 = {"entry_price": 100.0, "stop_price": 88.0, "high_water": 100.0}
    ns0, _ = le.trail_stop_price(lot0, caps0, 106.0)
    check("offset 0 lifts to entry exactly", ns0 == 100.0, ns0)


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


def test_settled_buying_power_is_broker_bp_not_double_counted():
    # Robinhood's cash-account buying_power ALREADY excludes unsettled sale proceeds, so settled_bp
    # IS the broker bp — our ledger is reported as `unsettled` but NOT subtracted (that double-counts).
    os.environ["CASH_SETTLEMENT_GUARD"] = "1"
    broker = {"buying_power": 402.68, "pending_deposits": 0.0}
    state = {"unsettled": [{"settle_date": "2026-06-11", "amount": 148.03, "symbol": "ABC"}]}
    sbp, uns = le.settled_buying_power(state, broker, "2026-06-10")
    check("settled == broker bp (no double-count)", sbp == 402.68, sbp)
    check("unsettled total still reported for the log", uns == 148.03, uns)
    # a matured sale (settle_date <= today) is pruned from the ledger
    state2 = {"unsettled": [{"settle_date": "2026-06-10", "amount": 300.0, "symbol": "ABC"}]}
    sbp2, uns2 = le.settled_buying_power(state2, broker, "2026-06-10")
    check("matured sale pruned", uns2 == 0.0 and not state2["unsettled"], (uns2, state2["unsettled"]))
    check("settled still == broker bp after prune", sbp2 == 402.68, sbp2)


def test_settled_guard_off_returns_raw_bp():
    os.environ["CASH_SETTLEMENT_GUARD"] = "0"
    broker = {"buying_power": 2064.0, "pending_deposits": 1000.0}
    state = {"unsettled": [{"settle_date": "2099-01-01", "amount": 500.0}]}
    sbp, uns = le.settled_buying_power(state, broker, "2026-06-08")
    check("guard off -> raw bp, nothing excluded", sbp == 2064.0 and uns == 0.0, (sbp, uns))
    os.environ["CASH_SETTLEMENT_GUARD"] = "1"


def test_pack_entries_uses_leftover_cash_for_smaller_buys():
    # settled $402: the old slot math (402 // 300 = 1) funded only ONE entry; size-aware packing
    # funds the big one AND a small one with the change left over, deferring only what truly doesn't
    # fit. Each $100 ask -> 1 share = $100.50 (0.5% marketable-limit markup); $300 -> 3 sh = $301.50.
    quotes = {"AAA": {"ask": 100.0}, "BBB": {"ask": 100.0}, "CCC": {"ask": 100.0}}
    ready = [("AAA", {"symbol": "AAA", "dollar_amount": 300}),
             ("BBB", {"symbol": "BBB", "dollar_amount": 108}),
             ("CCC", {"symbol": "CCC", "dollar_amount": 108})]
    to_run, deferred = le.pack_entries(ready, cash=402.0, exp_headroom=9999.0, pead_room=float("inf"),
                                       max_entries=5, quotes=quotes, caps=CAPS, books_on=False)
    check("packs big + small from one settled balance", [s for s, _ in to_run] == ["AAA", "BBB"], to_run)
    check("defers only the genuinely unfunded one", len(deferred) == 1 and deferred[0][0] == "CCC", deferred)
    check("defer reason keeps the settled-cash category",
          deferred[0][2].startswith("deferred: no settled cash"), deferred[0][2])


def test_pack_entries_respects_exposure_and_max_entries():
    quotes = {"AAA": {"ask": 100.0}, "BBB": {"ask": 100.0}}
    ready = [("AAA", {"symbol": "AAA", "dollar_amount": 100}),
             ("BBB", {"symbol": "BBB", "dollar_amount": 100})]
    # exposure headroom only fits one $100.50 lot
    to_run, deferred = le.pack_entries(ready, cash=9999.0, exp_headroom=150.0, pead_room=float("inf"),
                                       max_entries=5, quotes=quotes, caps=CAPS, books_on=False)
    check("exposure headroom caps to one", [s for s, _ in to_run] == ["AAA"], to_run)
    check("exposure defer category", deferred[0][2].startswith("deferred: exposure cap full"), deferred)
    # MAX_ENTRIES_PER_TICK hard cap regardless of available cash
    to_run2, deferred2 = le.pack_entries(ready, cash=9999.0, exp_headroom=9999.0, pead_room=float("inf"),
                                         max_entries=1, quotes=quotes, caps=CAPS, books_on=False)
    check("max_entries caps to one", len(to_run2) == 1 and len(deferred2) == 1, (to_run2, deferred2))
    check("max_entries category", deferred2[0][2].startswith("deferred: MAX_ENTRIES_PER_TICK"), deferred2)


def test_pack_entries_pead_ceiling_and_unsizable_passthrough():
    # a pead entry is bounded by the remaining pead-book room; an unsizable entry (1 share over the
    # per-name cap) passes through to execute_buy (admitted, no budget spent) so it emits the precise
    # reject there rather than a cash-defer here.
    quotes = {"PEAD": {"ask": 100.0}, "BIG": {"ask": 500.0}}
    ready = [("PEAD", {"symbol": "PEAD", "dollar_amount": 100, "book": "pead"}),  # $100.50 > pead_room 50
             ("BIG", {"symbol": "BIG", "dollar_amount": 100})]  # 1 sh $500 > MAX_POSITION_USD 300 -> unsizable
    to_run, deferred = le.pack_entries(ready, cash=9999.0, exp_headroom=9999.0, pead_room=50.0,
                                       max_entries=5, quotes=quotes, caps=CAPS, books_on=True)
    check("pead deferred by its book ceiling",
          any(d[0] == "PEAD" and d[2].startswith("deferred: pead book ceiling") for d in deferred), deferred)
    check("unsizable entry admitted for execute_buy to reject",
          ("BIG", ready[1][1]) in to_run, to_run)


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


def _fake_rh(place_fn, recent_fn=None):
    """Build a fake rh_mcp module for execute_buy tests: review always clear, place delegates to
    place_fn, recent_orders to recent_fn (default: empty)."""
    import types
    fake = types.ModuleType("rh_mcp")
    fake.review = lambda spec: {"review": {"data": {"order_checks": {}}}, "errors": {}}
    fake.place = place_fn
    fake.recent_orders = recent_fn or (lambda sym, created_at_gte=None: {"orders": {"data": {"orders": []}}, "errors": {}})
    return fake


def test_execute_buy_arms_stop_in_tick():
    """The core guarantee: a filled whole-share BUY arms a resting stop_market IN THE SAME TICK (not
    10 min later at the next reconcile). The stop is sized off the REAL average fill price."""
    calls = {"place": []}

    def _place(spec, ref_id):
        calls["place"].append(spec)
        if spec["side"] == "buy":  # echo back a FILLED buy (avg 50.05, 5 shares)
            return {"order": {"data": {"id": "buy1", "state": "filled",
                    "cumulative_quantity": "5.000000", "average_price": "50.05"}}, "errors": {}}
        return {"order": {"data": {"id": "stop1"}}, "errors": {}}  # the resting stop
    saved = sys.modules.get("rh_mcp")
    sys.modules["rh_mcp"] = _fake_rh(_place)
    os.environ["LIVE_ARMED"] = "1"
    try:
        state = {"lots": {}}
        broker = {"positions": {}, "quotes": {"ABC": {"ask": 50.0, "last": 49.9, "bid": 49.8}}}
        res = le.execute_buy("ABC", {"dollar_amount": 250, "reason": "x"}, state, broker, CAPS,
                             exposure=0.0, buying_power=1000.0, n_positions=2, day_pnl=0.0, log=(log := []))
        lot = state["lots"]["ABC"]
        # In-tick fill confirmation upgrades the result to status=filled with the real cost basis
        # (P6: placed != filled) — the trade history records a fill, not just an intent.
        check("buy filled (in-tick confirm)", res["status"] == "filled", res)
        check("result carries the real fill price", res.get("price") == 50.05, res)
        stops = [s for s in calls["place"] if s["type"] == "stop_market"]
        check("a resting stop_market was armed in-tick", len(stops) == 1, calls)
        # STOP_LOSS_PCT 4% off the real 50.05 fill -> 48.05, whole 5 shares, GTC
        check("stop sized off real fill (50.05*0.96=48.05)", stops[0]["stop_price"] == "48.05", stops)
        check("stop is whole-share GTC", stops[0]["quantity"] == "5" and stops[0]["time_in_force"] == "gtc", stops)
        check("lot holds the resting stop id", lot["resting_stop_order_id"] == "stop1", lot)
        check("lot is resting, not naked", lot["stop_type"] == "resting", lot)
        check("lot no longer pending after in-tick fill", lot.get("pending") is False, lot)
        check("lot entry price = real fill", lot["entry_price"] == 50.05, lot)
        check("result flags stop armed", res.get("stop_armed") is True, res)
        check("logged arm_stop_on_entry", any(e["event"] == "arm_stop_on_entry" for e in log), log)
    finally:
        os.environ.pop("LIVE_ARMED", None)
        sys.modules["rh_mcp"] = saved if saved is not None else sys.modules.pop("rh_mcp", None)


def test_execute_buy_rereads_fill_then_arms():
    """If the place echo is pre-fill (state=confirmed, cum=0) — the normal case for a marketable limit
    — execute_buy re-reads the order from broker truth, sees the fill, and arms the stop off it."""
    calls = {"place": []}

    def _place(spec, ref_id):
        calls["place"].append(spec)
        if spec["side"] == "buy":  # submission echo: not yet filled
            return {"order": {"data": {"id": "buy2", "state": "confirmed",
                    "cumulative_quantity": "0.000000", "average_price": None}}, "errors": {}}
        return {"order": {"data": {"id": "stop2"}}, "errors": {}}

    def _recent(sym, created_at_gte=None):  # broker truth: the buy has since filled
        return {"orders": {"data": {"orders": [
            {"id": "buy2", "symbol": sym, "side": "buy", "state": "filled",
             "cumulative_quantity": "5.000000", "average_price": "50.10"}]}}, "errors": {}}
    saved = sys.modules.get("rh_mcp")
    sys.modules["rh_mcp"] = _fake_rh(_place, _recent)
    os.environ["LIVE_ARMED"] = "1"
    try:
        state = {"lots": {}}
        broker = {"positions": {}, "quotes": {"ABC": {"ask": 50.0, "last": 49.9, "bid": 49.8}}}
        res = le.execute_buy("ABC", {"dollar_amount": 250, "reason": "x"}, state, broker, CAPS,
                             exposure=0.0, buying_power=1000.0, n_positions=2, day_pnl=0.0, log=(log := []))
        lot = state["lots"]["ABC"]
        stops = [s for s in calls["place"] if s["type"] == "stop_market"]
        check("stop armed after broker re-read", len(stops) == 1, calls)
        check("stop sized off re-read fill (50.10*0.96=48.10)", stops[0]["stop_price"] == "48.10", stops)
        check("lot resting after re-read", lot["resting_stop_order_id"] == "stop2" and lot["stop_type"] == "resting", lot)
        check("stop_armed flag set", res.get("stop_armed") is True, res)
    finally:
        os.environ.pop("LIVE_ARMED", None)
        sys.modules["rh_mcp"] = saved if saved is not None else sys.modules.pop("rh_mcp", None)


def test_execute_buy_unfilled_stays_pending():
    """If the buy isn't confirmed filled in-tick (echo AND re-read show 0 filled), no stop is placed,
    the lot stays pending, and reconcile remains the backstop — no naked stop attempt on 0 shares."""
    calls = {"place": []}

    def _place(spec, ref_id):
        calls["place"].append(spec)
        return {"order": {"data": {"id": "buy3", "state": "confirmed",
                "cumulative_quantity": "0.000000", "average_price": None}}, "errors": {}}

    def _recent(sym, created_at_gte=None):  # still unfilled on re-read
        return {"orders": {"data": {"orders": [
            {"id": "buy3", "symbol": sym, "side": "buy", "state": "confirmed",
             "cumulative_quantity": "0.000000", "average_price": None}]}}, "errors": {}}
    saved = sys.modules.get("rh_mcp")
    sys.modules["rh_mcp"] = _fake_rh(_place, _recent)
    os.environ["LIVE_ARMED"] = "1"
    try:
        state = {"lots": {}}
        broker = {"positions": {}, "quotes": {"ABC": {"ask": 50.0, "last": 49.9, "bid": 49.8}}}
        res = le.execute_buy("ABC", {"dollar_amount": 250, "reason": "x"}, state, broker, CAPS,
                             exposure=0.0, buying_power=1000.0, n_positions=2, day_pnl=0.0, log=(log := []))
        lot = state["lots"]["ABC"]
        check("no stop placed on an unfilled entry", not any(s["type"] == "stop_market" for s in calls["place"]), calls)
        check("lot stays pending", lot.get("pending") is True, lot)
        check("no resting stop id yet", lot.get("resting_stop_order_id") is None, lot)
        check("stop_armed flag is False", res.get("stop_armed") is False, res)
    finally:
        os.environ.pop("LIVE_ARMED", None)
        sys.modules["rh_mcp"] = saved if saved is not None else sys.modules.pop("rh_mcp", None)


def test_execute_buy_synthetic_when_stop_arm_fails():
    """Fill confirmed but the stop place fails -> the lot is NOT left bare: stop_price + qty are set
    (so the 1-min sentinel covers it) and stop_type degrades to synthetic for reconcile to re-arm."""
    calls = {"place": []}

    def _place(spec, ref_id):
        calls["place"].append(spec)
        if spec["side"] == "buy":
            return {"order": {"data": {"id": "buy4", "state": "filled",
                    "cumulative_quantity": "5.000000", "average_price": "50.05"}}, "errors": {}}
        return {"order": None, "errors": {"detail": "stop rejected"}}  # the stop arm FAILS
    saved = sys.modules.get("rh_mcp")
    sys.modules["rh_mcp"] = _fake_rh(_place)
    os.environ["LIVE_ARMED"] = "1"
    try:
        state = {"lots": {}}
        broker = {"positions": {}, "quotes": {"ABC": {"ask": 50.0, "last": 49.9, "bid": 49.8}}}
        res = le.execute_buy("ABC", {"dollar_amount": 250, "reason": "x"}, state, broker, CAPS,
                             exposure=0.0, buying_power=1000.0, n_positions=2, day_pnl=0.0, log=(log := []))
        lot = state["lots"]["ABC"]
        check("stop arm was attempted", any(s["type"] == "stop_market" for s in calls["place"]), calls)
        check("no resting id after failed arm", lot.get("resting_stop_order_id") is None, lot)
        check("degraded to synthetic (sentinel-coverable)", lot["stop_type"] == "synthetic", lot)
        check("synthetic stop level set", lot["stop_price"] == 48.05, lot)
        check("qty set so sentinel can watch", lot["qty"] == 5.0, lot)
        check("logged arm_stop_on_entry_failed", any(e["event"] == "arm_stop_on_entry_failed" for e in log), log)
    finally:
        os.environ.pop("LIVE_ARMED", None)
        sys.modules["rh_mcp"] = saved if saved is not None else sys.modules.pop("rh_mcp", None)


def test_lot_take_profit_pct_per_book_overlay():
    """Two-book v2.1 exit overlay: a disco lot harvests at DISCO_TAKE_PROFIT_PCT, but live only
    honours it once DISCO_EXITS_LIVE=1 (paper validates first). pead lots always keep the global
    let-run TP; unlabeled (pre-split) lots default to disco, like book_of()."""
    import live_execute as lx
    gated = {"TAKE_PROFIT_PCT": 40.0, "DISCO_TAKE_PROFIT_PCT": 10.0, "DISCO_EXITS_LIVE": 0}
    armed = {"TAKE_PROFIT_PCT": 40.0, "DISCO_TAKE_PROFIT_PCT": 10.0, "DISCO_EXITS_LIVE": 1}
    check("live gated -> disco keeps global TP", lx.lot_take_profit_pct({"book": "disco"}, gated) == 40.0)
    check("live armed -> disco harvests at 10", lx.lot_take_profit_pct({"book": "disco"}, armed) == 10.0)
    check("pead always let-run", lx.lot_take_profit_pct({"book": "pead"}, armed) == 40.0)
    check("unlabeled lot defaults to disco", lx.lot_take_profit_pct({}, armed) == 10.0)
    check("overlay unset -> global TP", lx.lot_take_profit_pct({"book": "disco"},
                                                               {"TAKE_PROFIT_PCT": 40.0}) == 40.0)


def test_trail_per_book_overlay():
    """A7/A9 moonshot-remnant lock: a disco lot rides DISCO_TRAIL_STOP_PCT@DISCO_TRAIL_ACTIVATE_PCT
    once DISCO_EXITS_LIVE=1; pead lots (and gated live) keep the global TRAIL_* rungs."""
    armed = {"STOP_LOSS_PCT": 12.0, "TRAIL_STOP_PCT": 15.0, "TRAIL_ACTIVATE_PCT": 20.0,
             "TRAIL_MIN_STEP_PCT": 0.0, "DISCO_TRAIL_STOP_PCT": 5.0,
             "DISCO_TRAIL_ACTIVATE_PCT": 10.0, "DISCO_EXITS_LIVE": 1}
    gated = {**armed, "DISCO_EXITS_LIVE": 0}
    disco = {"book": "disco", "entry_price": 100.0, "stop_price": 88.0, "high_water": 100.0}
    pead = {"book": "pead", "entry_price": 100.0, "stop_price": 88.0, "high_water": 100.0}
    # +12%: disco's tight trail is active (>=10) -> 112*0.95; pead's global rung (act 20) is not
    ns, hw = le.trail_stop_price(dict(disco), armed, 112.0)
    check("disco armed trails 5% from peak at +12", ns == 106.4 and hw == 112.0, (ns, hw))
    ns, _ = le.trail_stop_price(dict(pead), armed, 112.0)
    check("pead keeps global trail (inactive at +12)", ns is None, ns)
    ns, _ = le.trail_stop_price(dict(disco), gated, 112.0)
    check("live gated -> disco keeps global trail", ns is None, ns)
    ns, _ = le.trail_stop_price({**disco, "book": None}, armed, 112.0)
    check("unlabeled lot defaults to disco", ns == 106.4, ns)
    # pead global rung still works when it activates
    ns, _ = le.trail_stop_price(dict(pead), armed, 125.0)
    check("pead global trail at +25 -> 125*0.85", ns == 106.25, ns)


def test_execute_sell_partial_scale_out():
    """A scale-out trim sells the tier qty as a price-protected limit, marks the tiers taken,
    keeps the remnant lot (NOT engine-closed), ratchets the synthetic stop to breakeven after the
    first trim, and flags stop_type=synthetic until reconcile re-arms the resting stop."""
    import types
    calls = {"cancel": [], "place": []}
    fake = types.ModuleType("rh_mcp")
    fake.cancel = lambda oid: (calls["cancel"].append(oid), {"errors": {}})[1]
    fake.place = lambda spec, ref_id: (calls["place"].append(spec),
                                       {"order": {"data": {"id": "t1"}}, "errors": {}})[1]
    saved = sys.modules.get("rh_mcp")
    sys.modules["rh_mcp"] = fake
    os.environ["LIVE_ARMED"] = "1"
    try:
        state = {"lots": {"VELO": {"qty": 4, "entry_price": 100.0, "stop_price": 88.0,
                                   "resting_stop_order_id": "stop1", "stop_type": "resting",
                                   "book": "disco"}}}
        broker = {"positions": {"VELO": {"qty": 4.0, "avg_cost": 100.0}},
                  "quotes": {"VELO": {"bid": 111.9, "last": 112.0, "ask": 112.1}}}
        action = {"reason": "scale-out 75% at +12% (tier +10%)", "qty": 3.0, "scale_tiers": [10.0]}
        res = le.execute_sell("VELO", action, state, broker, CAPS, log := [])
        lot = state["lots"]["VELO"]
        check("trim cancelled the resting stop", "stop1" in calls["cancel"], calls)
        check("trim placed a LIMIT sell of 3", len(calls["place"]) == 1
              and calls["place"][0]["type"] == "limit" and calls["place"][0]["quantity"] == "3", calls)
        check("trim is not urgent", res.get("urgent") is False, res)
        check("tier marked taken", lot.get("scaled") == [10.0], lot)
        check("scale base remembered", lot.get("init_qty") == 4.0, lot)
        check("remnant qty tracked", lot.get("qty") == 1.0, lot)
        check("remnant NOT engine-closed", not lot.get("closing_order_id"), lot)
        check("first trim ratchets stop to breakeven", lot.get("stop_price") == 100.0, lot)
        check("remnant covered synthetically until re-arm",
              lot.get("stop_type") == "synthetic" and lot.get("resting_stop_order_id") is None, lot)
    finally:
        os.environ.pop("LIVE_ARMED", None)
        if saved is not None:
            sys.modules["rh_mcp"] = saved
        else:
            sys.modules.pop("rh_mcp", None)


def test_execute_sell_tier_whole_share_rounding():
    """Whole-share lots trim whole shares (floor); a trim that rounds below 1 share sells the whole
    lot instead — the tier degrades to a full take-profit on a 1-share lot."""
    import types
    calls = {"place": []}
    fake = types.ModuleType("rh_mcp")
    fake.cancel = lambda oid: {"errors": {}}
    fake.place = lambda spec, ref_id: (calls["place"].append(spec),
                                       {"order": {"data": {"id": "t2"}}, "errors": {}})[1]
    saved = sys.modules.get("rh_mcp")
    sys.modules["rh_mcp"] = fake
    os.environ["LIVE_ARMED"] = "1"
    try:
        # 5-share lot, 75% tier -> 3.75 floors to 3, remnant 2
        state = {"lots": {"AAA": {"qty": 5, "entry_price": 100.0, "stop_price": 88.0,
                                  "stop_type": "synthetic", "book": "disco"}}}
        broker = {"positions": {"AAA": {"qty": 5.0, "avg_cost": 100.0}},
                  "quotes": {"AAA": {"bid": 111.9, "last": 112.0, "ask": 112.1}}}
        le.execute_sell("AAA", {"reason": "scale-out", "qty": 3.75, "scale_tiers": [10.0]},
                        state, broker, CAPS, [])
        check("fractional tier qty floored to whole", calls["place"][-1]["quantity"] == "3", calls)
        check("remnant after floor", state["lots"]["AAA"]["qty"] == 2.0, state["lots"]["AAA"])
        # 1-share lot: 0.75 rounds below 1 -> sells the whole share (full close semantics)
        state2 = {"lots": {"BBB": {"qty": 1, "entry_price": 100.0, "stop_price": 88.0,
                                   "stop_type": "synthetic", "book": "disco"}}}
        broker2 = {"positions": {"BBB": {"qty": 1.0, "avg_cost": 100.0}},
                   "quotes": {"BBB": {"bid": 111.9, "last": 112.0, "ask": 112.1}}}
        le.execute_sell("BBB", {"reason": "scale-out", "qty": 0.75, "scale_tiers": [10.0]},
                        state2, broker2, CAPS, [])
        lot2 = state2["lots"]["BBB"]
        check("sub-share trim sells the whole lot", calls["place"][-1]["quantity"] == "1", calls)
        check("full-lot tier IS engine-closed", lot2.get("closing_order_id") == "t2", lot2)
        check("full-lot tier leaves no scaled marker", not lot2.get("scaled"), lot2)
    finally:
        os.environ.pop("LIVE_ARMED", None)
        if saved is not None:
            sys.modules["rh_mcp"] = saved
        else:
            sys.modules.pop("rh_mcp", None)


def test_disco_scale_out_tiers_parse():
    """tick_context.scale_out_tiers reads the per-book ladder from its own env key."""
    import tick_context as tc
    os.environ["DISCO_SCALE_OUT_TIERS"] = "10:0.75"
    os.environ["SCALE_OUT_TIERS"] = ""
    try:
        check("disco ladder parses", tc.scale_out_tiers("DISCO_SCALE_OUT_TIERS") == [(10.0, 0.75)])
        check("global ladder independent", tc.scale_out_tiers() == [])
    finally:
        os.environ.pop("DISCO_SCALE_OUT_TIERS", None)
        os.environ.pop("SCALE_OUT_TIERS", None)


def test_live_snapshot_shared_cash_parse():
    """Regression for the breaker bug: cash (full NAV leg) and buying_power (spendable) are DISTINCT on
    a cash account, and the executor + gate now parse them from the ONE shared module so they can't
    drift apart again."""
    import live_snapshot as ls
    snap = {"portfolio": {"data": {"cash": "1086.39",
            "buying_power": {"buying_power": "930.78", "unleveraged_buying_power": "930.78"},
            "pending_deposits": "0"}},
            "positions": {"data": {"positions": [
                {"symbol": "ABC", "quantity": "5", "average_buy_price": "50.05",
                 "shares_available_for_sells": "5"}]}}}
    port = ls.parse_portfolio(snap)
    check("buying_power = spendable leg", port["buying_power"] == 930.78, port)
    check("cash = FULL balance, not buying_power", port["cash"] == 1086.39, port)
    check("cash != buying_power (the breaker bug conflated them)", port["cash"] != port["buying_power"], port)
    # the executor's parse_snapshot must surface the SAME cash/bp (shared parser -> can't drift from gate)
    b = le.parse_snapshot(snap)
    check("executor parse_snapshot uses shared cash", b["cash"] == 1086.39, b)
    check("executor parse_snapshot uses shared buying_power", b["buying_power"] == 930.78, b)
    check("shared position parse", ls.parse_positions(snap)["ABC"]["qty"] == 5.0, ls.parse_positions(snap))
    # cash falls back to buying_power when the broker omits the cash field
    port2 = ls.parse_portfolio({"portfolio": {"data": {"buying_power": {"buying_power": "500"}}}})
    check("cash falls back to buying_power when absent", port2["cash"] == 500.0 and port2["buying_power"] == 500.0, port2)


def test_record_stop_adjustments_filters_and_writes():
    import json
    import tempfile
    import trade_log
    saved = (trade_log.STOPS_LOG, trade_log.JOURNAL_DIR)
    with tempfile.TemporaryDirectory() as tmp:
        trade_log.STOPS_LOG = Path(tmp) / "stops.jsonl"
        trade_log.JOURNAL_DIR = Path(tmp) / "journal"
        try:
            log = [
                {"event": "trail_rearm", "symbol": "abc", "from": 92.0, "to": 104.5,
                 "order_id": "s2", "ref_id": "r1"},
                {"event": "trail_rearm_dryrun", "symbol": "DEF", "from": 50.0, "to": 55.0,
                 "high_water": 58.0},
                {"event": "trail_rearm_failed", "symbol": "GHI", "from": 10.0, "to": 11.0,
                 "fallback": "synthetic"},
                {"event": "sell_placed", "symbol": "XYZ"},   # not a stop event -> skipped
                {"event": "arm_stop", "symbol": "JKL"},      # initial arm, not a ratchet -> skipped
            ]
            rows = trade_log.record_stop_adjustments(log, ts_utc="2026-06-12T14:00:00Z",
                                                     ts_et="2026-06-12T10:00:00", mode="live")
            check("only the three trail_* events are recorded", len(rows) == 3, rows)
            check("symbol is upper-cased", rows[0]["symbol"] == "ABC", rows[0])
            check("outcome maps applied", rows[0]["outcome"] == "applied", rows[0])
            check("outcome maps dryrun", rows[1]["outcome"] == "dryrun", rows[1])
            check("outcome maps failed", rows[2]["outcome"] == "failed", rows[2])
            written = [json.loads(x) for x in trade_log.STOPS_LOG.read_text().splitlines()]
            check("jsonl has the three rows", len(written) == 3, written)
            blotter = (trade_log.JOURNAL_DIR / "stops-2026-06-12.md").read_text()
            check("blotter shows the ratchet move", "ABC 92.0 → 104.5" in blotter, blotter)
        finally:
            trade_log.STOPS_LOG, trade_log.JOURNAL_DIR = saved


if __name__ == "__main__":
    tests = [test_whole_share_entry, test_rounds_up_to_one_share, test_one_share_over_cap_rejects,
             test_cap_rejects, test_sell_specs, test_review_gating, test_snapshot_parse_real_shapes,
             test_buying_power_fallback_to_cash, test_order_obj_and_place_failure,
             test_reconcile_books_close, test_reconcile_adopted_close_also_booked,
             test_reconcile_pending_not_booked_closed,
             test_reconcile_losing_exit_stamps_cooldown, test_reconcile_breakeven_exit_skips_cooldown,
             test_trail_off_by_default, test_trail_ratchets_up_and_never_down,
             test_trail_min_step_guard, test_trail_floored_at_initial_stop,
             test_breakeven_rung_lifts_to_entry, test_breakeven_offset_lifts_above_entry,
             test_breakeven_and_trail_compose,
             test_next_settle_date_skips_weekends, test_settled_buying_power_is_broker_bp_not_double_counted,
             test_settled_guard_off_returns_raw_bp,
             test_pack_entries_uses_leftover_cash_for_smaller_buys,
             test_pack_entries_respects_exposure_and_max_entries,
             test_pack_entries_pead_ceiling_and_unsizable_passthrough,
             test_reconcile_trails_resting_stop, test_reconcile_trail_dryrun_places_nothing,
             test_run_relays_parallel_overlaps_and_isolates,
             test_execute_sell_full_close_is_market, test_execute_sell_rearms_stop_on_failed_sell,
             test_reconcile_cancels_stranded_sell_then_arms,
             test_reconcile_adoption_distinct_costs,
             test_execute_buy_arms_stop_in_tick, test_execute_buy_rereads_fill_then_arms,
             test_execute_buy_unfilled_stays_pending, test_execute_buy_synthetic_when_stop_arm_fails,
             test_lot_take_profit_pct_per_book_overlay,
             test_trail_per_book_overlay, test_execute_sell_partial_scale_out,
             test_execute_sell_tier_whole_share_rounding, test_disco_scale_out_tiers_parse,
             test_live_snapshot_shared_cash_parse,
             test_record_stop_adjustments_filters_and_writes]
    for fn in tests:
        fn()
    print(f"OK — {_passed} assertions passed across {len(tests)} tests")
