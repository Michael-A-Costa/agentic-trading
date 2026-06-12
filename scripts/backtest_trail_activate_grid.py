#!/usr/bin/env python3
"""
backtest_trail_activate_grid.py — full trail% x activate% sweep (exit-strategy-findings §A13).

For every TRAIL in {3..20} x ACTIVATE in {5..20} (288 combos), replay the exit policy on the
LARGE/pead and MIDCAP/disco cohorts, holding the live protections fixed (stop12, tp40, be12).
Prints a mean heatmap, a median heatmap, and the top-10-by-mean per cohort. DAILY BARS — the
tight-trail (low TRAIL%) rows are flattered by daily resolution (they can't see intraday
whipsaw); treat the disco trail3-4@act7-9 'free lunch' cell as a hypothesis, not a result.
Tie-in: §A12's intraday tape test (exit_counterfactual.py --remnant) is what confirms or kills
the tight-trail rows at the 30-round-trip checkpoint. Run: python3 scripts/backtest_trail_activate_grid.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_exit_policy as bx

HOLD, COST = 15, 15.0
TRAILS = list(range(3, 21))      # 3..20
ACTS   = list(range(5, 21))      # 5..20
BASE = {"stop": 12, "tp": 40, "be": 12, "cost_bps": COST}  # live protections held fixed

cohorts = [
    ("LARGE/pead",   bx.find_entries(bx.gd.LARGE,  7.0, 2.0, HOLD, False)),
    ("MIDCAP/disco", bx.find_entries(bx.gd.MIDCAP, 4.0, 1.0, HOLD, False)),
]

for cname, entries in cohorts:
    # current live baseline = trail15 @ activate20
    base = bx.evalpol(entries, HOLD, {**BASE, "trail": 15, "activate": 20})
    print("=" * 92)
    print(f"TRAIL x ACTIVATE FULL GRID — {cname} | {len(entries)} entries | stop12 tp40 be12 | cost {COST}bps")
    print(f"  CURRENT LIVE (trail15@act20): mean {base['mean']*100:+.2f}%  median {base['median']*100:+.2f}%  win {base['win']*100:.0f}%")
    print("=" * 92)
    grid = {}
    for t in TRAILS:
        for a in ACTS:
            grid[(t, a)] = bx.evalpol(entries, HOLD, {**BASE, "trail": t, "activate": a})
    # MEAN heatmap
    print("\n  MEAN %/trade   rows=TRAIL%(below peak)   cols=ACTIVATE%(gain to arm)")
    print("  trail\\act " + "".join(f"{a:>6}" for a in ACTS))
    for t in TRAILS:
        print(f"  {t:>6}   " + "".join(f"{grid[(t,a)]['mean']*100:>+6.2f}" for a in ACTS))
    # MEDIAN heatmap
    print("\n  MEDIAN %/trade")
    print("  trail\\act " + "".join(f"{a:>6}" for a in ACTS))
    for t in TRAILS:
        print(f"  {t:>6}   " + "".join(f"{grid[(t,a)]['median']*100:>+6.2f}" for a in ACTS))
    # top-10 by mean
    rows = sorted(grid.items(), key=lambda kv: kv[1]['mean'], reverse=True)
    print(f"\n  TOP-10 by mean ({cname}):")
    for (t,a), s in rows[:10]:
        print(f"    trail{t:>2}@act{a:<2}  mean {s['mean']*100:+.2f}%  med {s['median']*100:+.2f}%  win {s['win']*100:.0f}%  sh {s['sharpe']:.3f}")
    print()
