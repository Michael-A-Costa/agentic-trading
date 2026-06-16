#!/usr/bin/env python3
"""
breakeven_counterfactual.py — INTRADAY replay of the breakeven rung on the 1-min sentinel tape.

Question (owner 2026-06-16): would lowering TRAIL_BREAKEVEN_AT_PCT (5 -> ~3.5/4) lock in small
gains / lose less money? exit_counterfactual.py can't answer it — it's daily-bar and the give-backs
we care about (AMKR +4.3%->red, ARQQ +4.0%->red) happen INTRADAY, same session. So replay on the
1-min quote tape (data/quotes-intraday.jsonl) instead.

Method (isolates the breakeven dial — everything else held constant):
  - FIFO-pair buys/sells in trades.jsonl into closed round-trips (per symbol, per mode).
  - Keep only round-trips whose hold window is COVERED by the tape (entry & exit minutes present).
  - For each breakeven threshold X in the grid, walk the lot's 1-min path and apply the SAME stop
    schedule the live engine uses, ratchet-only, highest engaged rung wins:
        catastrophe = entry*(1-STOP_LOSS_PCT/100)          [always]
        breakeven   = entry*(1+BE_OFFSET/100)  once peak>=X [the dial under test]
        trail       = peak*(1-TRAIL_STOP_PCT/100) once peak>=TRAIL_ACTIVATE_PCT
    First minute whose last <= current stop => exit at the stop. If no rung fires by the actual
    exit minute, the trade keeps its ACTUAL exit price (the discretionary/thesis exit is exogenous
    and identical across all X — so any delta is attributable to the breakeven dial alone).
  - Compare total realized $ across X. Baseline X=5 is today's live setting.

Honesty: tape 'last' per minute (no true high/low) slightly understates wicks, but identically for
every X, so the DELTA between thresholds is unbiased. Daily catastrophe/trail interplay is faithful.
Sample is only as deep as the tape (a few days) — this is DIRECTIONAL, not the >=30-RT decision gate.
"""
from __future__ import annotations
import json, os
from collections import defaultdict, deque
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TRADES = REPO / "data" / "trades.jsonl"
TAPE = REPO / "data" / "quotes-intraday.jsonl"

STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", "12"))
BE_OFFSET = float(os.environ.get("TRAIL_BREAKEVEN_OFFSET_PCT", "1.0"))
TRAIL_PCT = float(os.environ.get("DISCO_TRAIL_STOP_PCT", "3") or 3)
TRAIL_ACT = float(os.environ.get("DISCO_TRAIL_ACTIVATE_PCT", "7.5") or 7.5)
GRID = [5.0, 4.0, 3.5, 3.0]   # X=5 is the live baseline

def f(x):
    try: return float(x)
    except Exception: return 0.0   # 0 is filtered by the price/qty guards; keeps every value a float

def load_tape():
    """{sym: [(ts_et, price), ...]} sorted by ts."""
    t = defaultdict(list)
    for l in open(TAPE):
        if not l.strip(): continue
        try: r = json.loads(l)
        except Exception: continue
        ts = r.get("ts_et", "")
        for sym, px in (r.get("quotes") or {}).items():
            if px is not None: t[sym].append((ts, float(px)))
    for sym in t: t[sym].sort()
    return t

def round_trips():
    """FIFO-pair live fills into closed round-trips with timestamps + actual realized $."""
    fills = []
    for l in open(TRADES):
        if not l.strip(): continue
        try: r = json.loads(l)
        except Exception: continue
        if r.get("mode") not in (None, "live"): continue
        px = f(r.get("price")); qty = f(r.get("qty"))
        if not px or px <= 0 or not qty: continue   # skip null-price legs (booked on the paired row)
        fills.append(r)
    fills.sort(key=lambda r: r.get("ts_et", ""))
    lots = defaultdict(deque)   # sym -> deque of [qty, px, ts]
    rts = []
    for r in fills:
        sym = r.get("symbol"); px = f(r.get("price")); qty = f(r.get("qty")); ts = r.get("ts_et", "")
        if r.get("side") == "buy":
            lots[sym].append([qty, px, ts])
        else:
            rem = qty
            while rem > 1e-9 and lots[sym]:
                lot = lots[sym][0]
                take = min(rem, lot[0])
                rts.append({"sym": sym, "qty": take, "entry_px": lot[1], "entry_ts": lot[2],
                            "exit_px": px, "exit_ts": ts})
                lot[0] -= take; rem -= take
                if lot[0] <= 1e-9: lots[sym].popleft()
    return rts

def replay(rt, tape, X, offset, trail_act=TRAIL_ACT, trail_pct=TRAIL_PCT):
    """Return exit_return_fraction under breakeven threshold X / offset% and trail activate/width."""
    e = rt["entry_px"]
    path = [(ts, px) for ts, px in tape.get(rt["sym"], [])
            if rt["entry_ts"] <= ts <= rt["exit_ts"]]
    if not path:
        return (rt["exit_px"] / e - 1.0)        # no coverage -> actual
    cat = e * (1 - STOP_LOSS_PCT / 100.0)
    be = e * (1 + offset / 100.0)
    peak = e
    stop = cat
    for _ts, px in path:
        if px > peak: peak = px
        gain = (peak / e - 1.0) * 100.0
        s = cat
        if gain >= X: s = max(s, be)
        if gain >= trail_act: s = max(s, peak * (1 - trail_pct / 100.0))
        stop = max(stop, s)                      # ratchet-only, highest engaged rung wins
        if px <= stop:
            return (stop / e - 1.0)              # mechanical exit at the stop
    return (rt["exit_px"] / e - 1.0)             # no rung fired -> actual (exogenous) exit

def main():
    tape = load_tape()
    rts = round_trips()
    covered = [rt for rt in rts
               if any(rt["entry_ts"] <= ts <= rt["exit_ts"] for ts, _ in tape.get(rt["sym"], []))]
    print(f"closed round-trips: {len(rts)}   with intraday tape coverage: {len(covered)}")
    print(f"held constant: catastrophe -{STOP_LOSS_PCT:g}%  trail {TRAIL_PCT:g}%@act{TRAIL_ACT:g}%\n")
    notional = {id(rt): rt["entry_px"] * rt["qty"] for rt in covered}
    base = {id(rt): replay(rt, tape, 5.0, 1.0) for rt in covered}   # live config = BE_AT 5 / offset 1
    base_tot = sum(base[id(rt)] * notional[id(rt)] for rt in covered)

    AT_GRID = [5.0, 4.5, 4.0, 3.5, 3.0, 2.5, 2.0, 1.5]
    OFF_GRID = [0.5, 1.0, 1.5, 2.0]
    print(f"SWEEP — total $ over {len(covered)} covered RTs (live baseline BE_AT5/off1 = ${base_tot:.2f})")
    print(f"   {'BE_AT':>6} | " + " ".join(f"off{o:>4.1f}" for o in OFF_GRID) + "   | worst-clip$")
    best = (base_tot, 5.0, 1.0)
    for X in AT_GRID:
        row = []
        worst = 0.0  # most negative single changed-trade delta at offset 1.0 (premature-stop damage)
        for o in OFF_GRID:
            tot = 0.0
            for rt in covered:
                ret = replay(rt, tape, X, o)
                tot += ret * notional[id(rt)]
                if o == 1.0:
                    d = (ret - base[id(rt)]) * notional[id(rt)]
                    if d < worst: worst = d
            row.append(tot)
            if tot > best[0]:
                best = (tot, X, o)
        print(f"   {X:>6.1f} | " + " ".join(f"{v:>7.2f}" for v in row) + f"   | {worst:>+7.2f}")
    bt, bX, bo = best
    print(f"\n  BEST (BE_AT sweep): BE_AT={bX}  offset={bo}  -> ${bt:.2f}  (+${bt-base_tot:.2f} vs live)")

    # ---- TRAIL-ACTIVATE sweep, with BE_AT=3 / offset=1 fixed (owner: start trailing earlier so the
    # +3..+7.5% zone follows the stock up instead of parking the stop at breakeven+1%).
    print(f"\nTRAIL-ACTIVATE sweep @ BE_AT=3 / offset=1 / trail width {TRAIL_PCT:g}%  (live act = 7.5)")
    print(f"   {'ACT%':>6}{'total $':>12}{'vs BE3only':>12}{'worst-clip$':>13}")
    be3 = {id(rt): replay(rt, tape, 3.0, 1.0, 7.5) for rt in covered}
    be3_tot = sum(be3[id(rt)] * notional[id(rt)] for rt in covered)
    best_act = (be3_tot, 7.5)
    for act in [7.5, 6.0, 5.0, 4.5, 4.0, 3.5, 3.0]:
        tot = 0.0; worst = 0.0
        for rt in covered:
            ret = replay(rt, tape, 3.0, 1.0, act)
            tot += ret * notional[id(rt)]
            d = (ret - be3[id(rt)]) * notional[id(rt)]
            if d < worst: worst = d
        print(f"   {act:>6.1f}{tot:>12.2f}{tot-be3_tot:>+12.2f}{worst:>+13.2f}")
        if tot > best_act[0]: best_act = (tot, act)
    print(f"\n  BEST trail-activate: ACT={best_act[1]}  -> ${best_act[0]:.2f}  (+${best_act[0]-be3_tot:.2f} vs BE3-only)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
