#!/usr/bin/env python3
"""test_entry_quality_report.py — dependency-free tests for the Phase 0 join + bucketing logic.

Run:  python3 scripts/test_entry_quality_report.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entry_quality_report as eqr  # noqa: E402

_passed = 0


def check(name: str, cond: bool, extra: object = "") -> None:
    global _passed
    if not cond:
        print(f"FAIL: {name} {extra}")
        raise SystemExit(1)
    _passed += 1


def test_parse_dt_both_shapes():
    a = eqr._parse_dt("2026-06-15 14:07:13")          # broker truth, naive UTC
    b = eqr._parse_dt("2026-06-15T17:38:15+00:00")    # buy row, tz-aware
    check("broker-truth ts parsed", a is not None and a.tzinfo is not None, a)
    check("buy-row ts parsed", b is not None and b.tzinfo is not None, b)
    check("bad ts -> None", eqr._parse_dt("not a date") is None)


def test_iv_bucket_edges():
    check("iv None dropped", eqr._iv_bucket(None) is None)
    check("iv 55 -> <60", eqr._iv_bucket(55) == "iv<60", eqr._iv_bucket(55))
    check("iv 60 -> 60-90", eqr._iv_bucket(60) == "iv60-90", eqr._iv_bucket(60))
    check("iv 152 -> 120+", eqr._iv_bucket(152) == "iv120+", eqr._iv_bucket(152))


def test_match_entry_nearest_within_window():
    buys = {"BRUN": [
        (datetime(2026, 6, 15, 17, 30, tzinfo=timezone.utc), {"conviction": "high"}),
        (datetime(2026, 6, 15, 17, 42, tzinfo=timezone.utc), {"conviction": "low"}),   # the real one
    ]}
    # broker entry fill at 17:42:50 -> should match the 17:42 low-conviction buy, not the 17:30
    m = eqr.match_entry("BRUN", "2026-06-15 17:42:50", buys, window_min=10)
    check("matched nearest buy", m == {"conviction": "low"}, m)
    # an entry 40 min from any buy -> no match
    check("no match beyond window", eqr.match_entry("BRUN", "2026-06-15 19:00:00", buys, 10) is None)
    check("unknown symbol -> None", eqr.match_entry("ZZZZ", "2026-06-15 17:42:00", buys, 10) is None)


def test_stats_pf_winrate_and_drop2():
    rows = [{"realized_usd": x} for x in (10.0, 4.0, -2.0, -3.0, -5.0)]  # gw=14, gl=10
    s = eqr.stats(rows)
    check("n", s["n"] == 5, s)
    check("realized sum", abs(s["realized"] - 4.0) < 1e-9, s)
    check("win%", abs(s["win_pct"] - 40.0) < 1e-9, s)
    check("profit factor 1.4", abs(s["pf"] - 1.4) < 1e-9, s)
    # drop the 2 best (10, 4) -> -2-3-5 = -10 : exposes a loss masked by 2 winners
    check("drop-top-2 realized", abs(s["drop2"] - (-10.0)) < 1e-9, s)


def test_stats_all_wins_pf_infinite():
    s = eqr.stats([{"realized_usd": 2.0}, {"realized_usd": 3.0}])
    check("no losses -> PF None (∞)", s["pf"] is None, s)
    check("avg_loss 0 when no losses", s["avg_loss"] == 0.0, s)


if __name__ == "__main__":
    tests = [test_parse_dt_both_shapes, test_iv_bucket_edges, test_match_entry_nearest_within_window,
             test_stats_pf_winrate_and_drop2, test_stats_all_wins_pf_infinite]
    for fn in tests:
        fn()
    print(f"OK — {_passed} assertions passed across {len(tests)} tests")
