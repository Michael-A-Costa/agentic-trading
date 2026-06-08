#!/usr/bin/env python3
"""test_hold_risk.py — regression tests for the Tier-1 holding risk score (hold_risk.score).

Focus: the proximity-to-stop term must NOT blow up once a TRAILING stop ratchets to/above the
entry price. Before the fix, (last-stop)/(entry-stop) divided by a negative span and flagged a
green winner CRITICAL, force-selling it at the next tick. These lock that down and prove the
losing-position behaviour is unchanged.

Run:  python3 scripts/test_hold_risk.py     (exits non-zero on first failure)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hold_risk  # noqa: E402

NOW = datetime.now(timezone.utc)
_passed = 0


def check(name: str, cond: bool, extra=None) -> None:
    global _passed
    if not cond:
        print(f"FAIL: {name} {extra if extra is not None else ''}")
        raise SystemExit(1)
    _passed += 1


def test_trailing_winner_not_critical():
    # the exact case the live-trace demo surfaced: +12% winner whose TRAILING stop has ratcheted
    # ABOVE entry (21.15 > 20.05). Must NOT be critical and must NOT protective-sell.
    pos = {"entry_price": 20.05, "stop_price": 21.15, "last": 22.50, "pnl_pct": 12.2,
           "conviction": "high", "hold_intent": "runner", "range_pos": 0.6, "intraday_pct": 1.0}
    r = hold_risk.score(pos, NOW)
    check("trailing winner not critical", r["band"] != "critical", r)
    check("trailing winner no protective sell", r["protective_sell"] is False, r)
    check("proximity term contributes ~0 for green lot", r["risk"] < 25, r)


def test_early_trail_above_entry_below_cost_stop():
    # stop still below entry (19.83 < 20.05) but price is green (21.10) — proximity must be ~0,
    # not a penalty against the position's own high.
    pos = {"entry_price": 20.05, "stop_price": 19.83, "last": 21.10, "pnl_pct": 5.2,
           "conviction": "high", "hold_intent": "runner", "range_pos": 0.6, "intraday_pct": 1.0}
    r = hold_risk.score(pos, NOW)
    check("early-trail green lot is low risk", r["band"] == "low", r)
    check("early-trail no protective sell", r["protective_sell"] is False, r)


def test_losing_position_score_preserved():
    # a fader approaching its (sub-entry) stop while still falling: the smart soft-cut MUST fire,
    # exactly as before the fix (ref_high collapses to entry when last<entry -> original formula).
    pos = {"entry_price": 20.05, "stop_price": 18.45, "last": 19.05, "pnl_pct": -4.99,
           "conviction": "medium", "hold_intent": "swing", "range_pos": 0.1, "intraday_pct": -3.0}
    r = hold_risk.score(pos, NOW)
    check("falling loser protective-sells", r["protective_sell"] is True, r)
    check("falling loser scores elevated", r["risk"] >= 45, r)


def test_loser_proximity_matches_original_formula():
    # proximity term in isolation must equal the original (last-stop)/(entry-stop) for a loser.
    # entry 20.05, stop 18.45, last 19.25: prox = (19.25-18.45)/(20.05-18.45) = 0.5 -> term = 20.
    # Strip the other terms (flat intraday, mid-range, high conviction) so risk == the prox term.
    pos = {"entry_price": 20.05, "stop_price": 18.45, "last": 19.25, "pnl_pct": -4.0,
           "conviction": "high", "hold_intent": "runner", "range_pos": 0.6, "intraday_pct": 0.0}
    r = hold_risk.score(pos, NOW)
    # pnl term adds min(4,10)*2 = 8; prox term adds (1-0.5)*40 = 20 -> 28 total.
    check("loser proximity unchanged (prox 0.5 -> 20 + pnl 8 = 28)", abs(r["risk"] - 28.0) < 1e-6, r)


def test_at_entry_baseline_low():
    # freshly filled, last == entry, stop 8% below: proximity prox=1 -> term 0; nothing else hot.
    pos = {"entry_price": 20.05, "stop_price": 18.45, "last": 20.05, "pnl_pct": 0.0,
           "conviction": "high", "hold_intent": "runner", "range_pos": 0.6, "intraday_pct": 0.0}
    r = hold_risk.score(pos, NOW)
    check("at-entry baseline is low risk", r["band"] == "low" and r["risk"] == 0.0, r)


if __name__ == "__main__":
    tests = [test_trailing_winner_not_critical, test_early_trail_above_entry_below_cost_stop,
             test_losing_position_score_preserved, test_loser_proximity_matches_original_formula,
             test_at_entry_baseline_low]
    for fn in tests:
        fn()
    print(f"OK — {_passed} assertions passed across {len(tests)} tests")
