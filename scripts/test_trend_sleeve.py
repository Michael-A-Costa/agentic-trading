#!/usr/bin/env python3
"""
test_trend_sleeve.py — dependency-free unit tests for the BTC trend sleeve's PURE rules (signal,
sizing, stop, re-entry lock, breaker, resize, settlement), plus a parity check that the live signal
matches bakeoff_crypto's SMA filter on every historical day when the cached candles are present.

Run:  python3 scripts/test_trend_sleeve.py     (exits non-zero on first failure)
"""
from __future__ import annotations

import csv
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import trend_sleeve as ts  # noqa: E402

CFG = {"risk_pct": 2.0, "stop_pct": 8.0, "max_frac": 1.0, "limit_pct": 0.5}
_passed = 0


def check(name: str, cond: bool, extra: object = "") -> None:
    global _passed
    if not cond:
        print(f"FAIL: {name} {extra}")
        sys.exit(1)
    _passed += 1


def closes_from(prices: list[float], start: date = date(2026, 1, 1)) -> list:
    return [(start + timedelta(days=i), p) for i, p in enumerate(prices)]


# --- completed_closes drops today's forming candle and sorts oldest-first
today = date(2026, 9, 23)
def _ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
candles = [[_ts(today), 1, 1, 1, 999.0, 1], [_ts(today - timedelta(days=1)), 1, 1, 1, 10.0, 1],
           [_ts(today - timedelta(days=2)), 1, 1, 1, 9.0, 1]]
cc = ts.completed_closes(candles, today)
check("drops forming candle", [c for _, c in cc] == [9.0, 10.0], cc)

# --- signal on/off and extension
up = closes_from([100 + i for i in range(60)])
s = ts.compute_signal(up, 50, 30, 0.5)
check("uptrend is on", s["ok"] and s["on"])
check("weight capped at 1 in low vol", s["weight"] == 1.0, s)
down = closes_from([200 - i for i in range(60)])
s = ts.compute_signal(down, 50, 30, 0.5)
check("downtrend is off, weight 0", s["ok"] and not s["on"] and s["weight"] == 0.0, s)
check("too little history", not ts.compute_signal(up[:40], 50, 30, 0.5)["ok"])
wild = closes_from([100 * (1.10 if i % 2 else 0.95) ** (i % 2) + i for i in range(60)])
s = ts.compute_signal(wild, 50, 30, 0.5)
check("high vol scales weight below 1", s["on"] and 0 < s["weight"] < 1, s)

# --- sizing: equity x budget / stop, whole shares, bounded by spendable
q, _ = ts.target_shares(equity=5000, weight=1.0, price=49.0, cfg=CFG, spendable=5000)
check("2% risk / 8% stop -> 25% position", q == math.floor(1250 / (49 * 1.005)), q)
q, _ = ts.target_shares(equity=100, weight=1.0, price=49.0, cfg=CFG, spendable=100)
check("$100 at 2% risk buys 0 whole shares", q == 0, q)
q, _ = ts.target_shares(equity=100, weight=1.0, price=49.0, cfg={**CFG, "risk_pct": 8.0}, spendable=100)
check("learning tier (8% budget) buys 2 shares", q == 2, q)
q, _ = ts.target_shares(equity=100, weight=1.0, price=49.0, cfg={**CFG, "risk_pct": 8.0}, spendable=60)
check("bounded by spendable cash", q == 1, q)
q, _ = ts.target_shares(equity=5000, weight=0.0, price=49.0, cfg=CFG, spendable=5000)
check("weight 0 -> flat", q == 0)
q, _ = ts.target_shares(equity=5000, weight=1.0, price=49.0, cfg={**CFG, "risk_pct": 20.0}, spendable=5000)
check("max_frac caps position at equity", q * 49 * 1.005 <= 5000, q)

# --- stop level
check("8% stop", ts.stop_level(50.0, 8.0) == 46.0)

# --- re-entry lock: must see OFF then ON
lock = {"since": "2026-09-01", "seen_below": False}
check("lock holds while still on", ts.update_lock(lock, True) == lock)
l2 = ts.update_lock(lock, False)
check("lock records the dip", l2["seen_below"] is True)
check("lock clears on the fresh cross", ts.update_lock(l2, True) is None)
check("no lock stays none", ts.update_lock(None, True) is None)

# --- breaker
check("breaker trips below 85% of hwm", ts.breaker_tripped(84.0, 100.0, 15.0))
check("breaker quiet at 86%", not ts.breaker_tripped(86.0, 100.0, 15.0))
check("breaker needs an hwm", not ts.breaker_tripped(50.0, None, 15.0))

# --- resize only on Mondays and only by a meaningful amount
check("no resize off-Monday", not ts.resize_warranted(10, 14, False, 0.2))
check("resize 10->14 Monday", ts.resize_warranted(10, 14, True, 0.2))
check("ignore 10->11 drift", not ts.resize_warranted(10, 11, True, 0.2))
check("1-share lot needs a full share", ts.resize_warranted(1, 2, True, 0.2))
check("never 'resize' into flat", not ts.resize_warranted(3, 0, True, 0.2))

# --- settlement: own proceeds not spendable until the next business day
unsettled = [{"amount": 98.0, "settles": "2026-09-28"}]  # sold Fri 9/25 -> settles Mon 9/28
check("friday sale settles monday", ts.next_business_day(date(2026, 9, 25)) == date(2026, 9, 28))
check("unsettled proceeds excluded", ts.settled_cash(100.0, unsettled, date(2026, 9, 25)) == 2.0)
check("settled on settle date", ts.settled_cash(100.0, unsettled, date(2026, 9, 28)) == 100.0)

# --- RH nanosecond timestamps
t = ts.parse_ts("2026-09-22T19:59:59.527129485Z")
check("nanosecond ts parses", t == datetime(2026, 9, 22, 19, 59, 59, 527129, tzinfo=timezone.utc), t)
check("plain ts parses", ts.parse_ts("2026-09-23T04:57:51Z").tzinfo is not None)

# --- parity with the backtest: same on/off as bakeoff_crypto's `close > SMA50` on every day
cache = Path(__file__).resolve().parent.parent / "data" / "bakeoff" / "crypto" / "BTC-USD.csv"
if cache.exists():
    rows = [(date.fromisoformat(r["date"]), float(r["close"])) for r in csv.DictReader(cache.open())]
    mismatches = 0
    for i in range(60, len(rows)):
        window = rows[: i + 1]
        live = ts.compute_signal(window, 50, 30, 0.5)["on"]
        sma = sum(c for _, c in window[-50:]) / 50
        mismatches += live != (window[-1][1] > sma)
    check(f"signal parity with backtest over {len(rows) - 60} days", mismatches == 0, mismatches)
else:
    print("skip: parity check (no cached BTC candles — run bakeoff_crypto.py first)")

# --- hourly status anomalies
CFGS = {"breaker_pct": 15.0}
stop_o = {"symbol": "IBIT", "side": "sell", "state": "confirmed", "stop_price": "45.00", "quantity": "2"}
def brk(pos, orders):
    return {"positions": pos, "orders": orders, "quotes": {}}
good = ts.status_anomalies("IBIT", brk({"IBIT": {"qty": 2}}, [stop_o]), {"lot": {"qty": 2}, "hwm": 100}, CFGS, 99)
check("healthy account has no anomalies", good == [], good)
bad = ts.status_anomalies("IBIT", brk({"IBIT": {"qty": 2}}, []), {"lot": {"qty": 2}}, CFGS, 99)
check("missing stop flagged", any("NO resting stop" in a for a in bad), bad)
bad = ts.status_anomalies("IBIT", brk({"IBIT": {"qty": 2}, "AAPL": {"qty": 1}}, [stop_o]), {"lot": {"qty": 2}}, CFGS, 99)
check("stray position flagged", any("AAPL" in a for a in bad), bad)
bad = ts.status_anomalies("IBIT", brk({"IBIT": {"qty": 3}}, [stop_o]), {"lot": {"qty": 2}}, CFGS, 99)
check("qty mismatches flagged", len(bad) == 2, bad)
bad = ts.status_anomalies("IBIT", brk({}, []), {"lot": None, "hwm": 200}, CFGS, 100)
check("breaker flagged", any("breaker" in a for a in bad), bad)
check("flat account is fine", ts.status_anomalies("IBIT", brk({}, []), {"lot": None, "hwm": 100}, CFGS, 100) == [])

print(f"OK — {_passed} checks passed")
