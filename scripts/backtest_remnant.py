#!/usr/bin/env python3
"""One-off: 'moonshot remnant' ladders (sell 75-90% at +10, trail the rest) on MOV-M.

Per-trade metrics + TWO 6-slot portfolio sims:
  port_x   — original megasweep accounting (slot busy until last share exits; pessimistic for ladders)
  port_pr  — partial-release accounting (each sold fraction frees its share of the slot that day;
             models settled-cash recycling, still ignoring T+1 like the original)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_gap_drift as gd
import backtest_exit_policy as bx
import backtest_quickwin as qw
from backtest_sweeps import find_mover_entries

GAP, HOLD, COST, SLOTS = 7.0, 15, 15.0, 6


def simulate_legs(bars, i, entry, hold, pol):
    """qw.simulate clone that records each sell leg as (day, frac, gross_leg_return)."""
    cat = pol.get("stop"); tp = pol.get("tp")
    trail = pol.get("trail") or 0.0; act = pol.get("activate", 0.0)
    be = pol.get("be"); softcut = pol.get("softcut")
    tiers = sorted(pol.get("tiers") or [], key=lambda t: t[0])
    C = pol.get("cost_bps", 15.0)
    stop_px = entry * (1 - cat / 100.0) if cat else None
    tp_px = entry * (1 + tp / 100.0) if tp else None
    floor = stop_px if stop_px is not None else -1e18
    hw = entry; remaining = 1.0; fired = set()
    legs_out = []

    def sell(frac, px, day):
        nonlocal remaining
        frac = min(frac, remaining)
        if frac <= 1e-9:
            return
        legs_out.append((day, frac, px / entry - 1.0))
        remaining -= frac

    for k in range(1, hold + 1):
        o, h, l = bars[i + k]["open"], bars[i + k]["high"], bars[i + k]["low"]
        if remaining > 0 and stop_px is not None and o <= stop_px:
            sell(remaining, o, k); break
        if remaining > 0 and stop_px is not None and l <= stop_px:
            sell(remaining, stop_px, k); break
        for ti, (g, frac) in enumerate(tiers):
            if ti in fired or remaining <= 0:
                continue
            tier_px = entry * (1 + g / 100.0)
            if h >= tier_px:
                fired.add(ti)
                sell(frac, max(tier_px, o) if o > tier_px else tier_px, k)
        if remaining > 0 and tp_px is not None and h >= tp_px:
            sell(remaining, max(tp_px, o) if o > tp_px else tp_px, k); break
        c = bars[i + k]["close"]
        if remaining > 0 and softcut is not None and c <= entry * (1 - softcut / 100.0) and c < o:
            sell(remaining, c, k); break
        if h > hw:
            hw = h
        gain = (hw / entry - 1.0) * 100.0
        cands = [floor]
        if be is not None and be > 0 and gain >= be:
            cands.append(entry)
        if trail > 0 and gain >= act:
            cands.append(hw * (1 - trail / 100.0))
        nxt = max(cands)
        if stop_px is None or nxt > stop_px:
            stop_px = nxt
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        sell(remaining, bars[i + hold]["close"], hold)
    # per-leg cost; entry cost (1 leg) charged pro-rata across sell fractions
    legs_net = [(d, f, r - 2 * C / 10000.0) for d, f, r in legs_out]  # f*C sell + f*C entry share
    return legs_net


def eval_pol(entries, pol):
    rets, peaks, sims, legsims = [], [], [], []
    for _, bars, i, entry in entries:
        r, d, pk = qw.simulate(bars, i, entry, HOLD, pol)
        rets.append(r); peaks.append(pk)
        sims.append((bars[i]["date"], bars[i + d]["date"], r))
        legs = simulate_legs(bars, i, entry, HOLD, pol)
        legsims.append((bars[i]["date"], [(bars[i + d2]["date"], f, lr) for d2, f, lr in legs]))
    n = len(rets)
    m, sd = gd.stats(rets)
    met = {"mean": m, "median": gd.median(rets), "win": sum(1 for x in rets if x > 0) / n,
           "gaveback": sum(1 for r, pk in zip(rets, peaks) if pk >= 5.0 and r < 0.02) / n}
    # original port: slot busy till flat
    sims.sort()
    equity, busy = 1.0, []
    for ed, xd, r in sims:
        busy = [(b, a, br) for b, a, br in busy if not (b <= ed and (equity := equity + a * br))]
        if len(busy) < SLOTS:
            busy.append((xd, equity / SLOTS, r))
    for _, a, br in busy:
        equity += a * br
    met["port_x"] = equity
    # partial-release port: each leg frees frac*alloc and books its profit on its date
    legsims.sort()
    eq, active = 1.0, []          # active: list of (release_date, occ_frac, alloc, leg_ret)
    occ = 0.0
    for ed, legs in legsims:
        keep = []
        for rd, f, a, lr in active:
            if rd <= ed:
                eq += a * f * lr; occ -= f
            else:
                keep.append((rd, f, a, lr))
        active = keep
        if occ <= SLOTS - 1.0 + 1e-9:
            alloc = eq / SLOTS
            occ += 1.0
            for rd, f, lr in legs:
                active.append((rd, f, alloc, lr))
    for _, f, a, lr in active:
        eq += a * f * lr
    met["port_pr"] = eq
    return met


def main():
    movers = qw.dedupe_entries(find_mover_entries(gd.MIDCAP, GAP, HOLD, "all"), HOLD)
    print(f"MOV-M cohort: {len(movers)} entries\n")
    base = {"stop": 12, "softcut": 8, "be": 12, "cost_bps": COST}
    configs = [
        ("TP10 (live)",            {**base, "tp": 10}),
        ("TP15 + tr12@12 (A2)",    {**base, "tp": 15, "trail": 12, "activate": 12}),
        ("75%@10 + tr12@12",       {**base, "tiers": [(10, 0.75)], "trail": 12, "activate": 12}),
        ("75%@10 + tr10@10",       {**base, "tiers": [(10, 0.75)], "trail": 10, "activate": 10}),
        ("75%@10 + tr15@12",       {**base, "tiers": [(10, 0.75)], "trail": 15, "activate": 12}),
        ("75%@10, be12 only",      {**base, "tiers": [(10, 0.75)]}),
        ("90%@10 + tr12@12",       {**base, "tiers": [(10, 0.90)], "trail": 12, "activate": 12}),
        ("50%@10 + tr12@12 (ref)", {**base, "tiers": [(10, 0.50)], "trail": 12, "activate": 12}),
    ]
    hdr = f"{'config':<26}{'mean':>8}{'median':>8}{'win':>6}{'gvbk':>6}{'port_x':>9}{'port_pr':>9}"
    print(hdr); print("-" * len(hdr))
    for name, pol in configs:
        m = eval_pol(movers, pol)
        print(f"{name:<26}{m['mean']*100:>+7.2f}%{m['median']*100:>+7.2f}%{m['win']*100:>5.0f}%"
              f"{m['gaveback']*100:>5.0f}%{m['port_x']:>8.2f}x{m['port_pr']:>8.2f}x")


if __name__ == "__main__":
    main()
