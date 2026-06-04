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


def check(name: str, cond: bool, extra: str = "") -> None:
    global _passed
    if not cond:
        print(f"FAIL: {name} {extra}")
        raise SystemExit(1)
    _passed += 1


def test_whole_share_entry():
    p = le.size_entry(280, {"ask": 50.0, "last": 49.9}, CAPS, canary_usd=None)
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


def test_fractional_entry():
    p = le.size_entry(100, {"ask": 250.0, "last": 249.0}, CAPS, canary_usd=None)
    check("frac kind market", p["kind"] == "market", p)
    check("frac synthetic stop", p["stop_type"] == "synthetic")
    spec = le.buy_spec("HI", p)
    check("frac buy_spec dollar market", spec["type"] == "market" and spec["dollar_amount"] == "100.00")
    check("frac buy_spec has no limit", "limit_price" not in spec)


def test_canary_caps_notional():
    p = le.size_entry(280, {"ask": 50.0}, CAPS, canary_usd=20)
    # $20 / $50 < 1 share -> fractional market for $20
    check("canary notional capped", p["notional"] == 20, p)
    check("canary flagged", p["canary_capped"] is True)
    check("canary forces fractional", p["kind"] == "market")


def test_cap_rejects():
    p = le.size_entry(280, {"ask": 50.0}, CAPS, canary_usd=None)
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


if __name__ == "__main__":
    tests = [test_whole_share_entry, test_fractional_entry, test_canary_caps_notional,
             test_cap_rejects, test_sell_specs, test_review_gating, test_snapshot_parse_real_shapes,
             test_buying_power_fallback_to_cash, test_order_obj_and_place_failure]
    for fn in tests:
        fn()
    print(f"OK — {_passed} assertions passed across {len(tests)} tests")
