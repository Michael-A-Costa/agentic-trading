#!/usr/bin/env python3
"""
backtest_sweeps.py — the playbook §11 backlog campaign, runnable as one command.

Sweeps the EXIT side of the gap-drift entry across every axis the playbook flags: tier-gain,
trim-fraction, two-tier ladders, hold horizon, entry definition, costs, whole-share lot rounding
at small equity, SPY-regime conditioning, and year-by-year stability. Entry definition, cost
model, and `simulate` are IMPORTED from backtest_exit_policy / backtest_quickwin — nothing is
redefined, so every number here is comparable with the playbook's §6 tables.

Entries are DEDUPED by default (overlapping same-symbol entries dropped — live can't re-enter a
held name); pass --overlap to reproduce the original overlapping entry set.

Usage:
  python3 scripts/backtest_sweeps.py --mode all
  python3 scripts/backtest_sweeps.py --mode tiergain --boot 2000
  python3 scripts/backtest_sweeps.py --mode regime --universe MIDCAP
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_gap_drift as gd
import backtest_exit_policy as bx
import backtest_quickwin as qw

BASE = {"stop": 12, "softcut": 8, "be": 12}     # the global protection layer (playbook §6b)


def P(**kw):
    d = dict(BASE)
    d.update(kw)
    return d


# The four exit FAMILIES every sensitivity axis is judged on (let-run reference first):
HEADLINE = [
    ("LET-RUN tp40 tr15@20", P(tp=40, trail=15, activate=20)),
    ("tight TP10",           P(tp=10)),
    ("scale 50%@5 + run",    P(tp=40, trail=15, activate=20, tiers=[(5, 0.5)])),
    ("scale 50%@8 + tr12@12", P(tp=40, trail=12, activate=12, tiers=[(8, 0.5)])),
]

UNIVERSES = [("LARGE", gd.LARGE), ("MIDCAP", gd.MIDCAP)]


ENTRY_STYLE = "gap"          # set from --entry; "movers" = the disco discovery screen


def get_entries(u, gap, vol, hold, dedupe, refresh=False):
    if ENTRY_STYLE == "movers":
        e = find_mover_entries(u, gap, hold, "all", refresh)
    else:
        e = bx.find_entries(u, gap, vol, hold, refresh)
    return qw.dedupe_entries(e, hold) if dedupe else e


def fmt(name, r, extra=""):
    return (f"  {name:<30}{r['mean']*100:>+6.2f}%{r['median']*100:>+7.2f}%{r['win']*100:>5.0f}%"
            f"{r['sharpe']:>7.3f}{r['p10']*100:>+7.1f}%{r['gaveback']*100:>7.0f}%{r['days']:>6.1f}{extra}")


HDR = (f"  {'policy':<30}{'mean':>7}{'median':>8}{'win%':>6}{'sharpe':>7}{'p10':>8}{'gavebk':>8}{'days':>6}")


def run_table(title, entries, hold, policies, cost, boot=0):
    print(f"\n--- {title} ({len(entries)} entries) ---")
    print(HDR + ("   mean-diff CI vs row1" if boot else ""))
    ref = None
    for name, pol in policies:
        r = qw.evalpol(entries, hold, {**pol, "cost_bps": cost}, want_rets=bool(boot))
        extra = ""
        if boot:
            if ref is None:
                ref, extra = r["rets"], "   (reference)"
            else:
                (mlo, mhi), _ = qw.boot_ci(ref, r["rets"], boot)
                extra = f"   [{mlo*100:+.2f}%, {mhi*100:+.2f}%]{'*' if (mlo > 0 or mhi < 0) else ''}"
        print(fmt(name, r, extra))


# ---------------------------------------------------------------- sweep modes

def mode_tiergain(args):
    pols = [HEADLINE[0]] + [
        (f"scale 50%@{g} + run", P(tp=40, trail=15, activate=20, tiers=[(g, 0.5)]))
        for g in (3, 4, 5, 6, 8, 10, 12)
    ]
    for uname, u in UNIVERSES:
        e = get_entries(u, args.gap, args.vol_mult, args.hold, args.dedupe)
        run_table(f"TIER-GAIN GRID — {uname}", e, args.hold, pols, args.cost_bps, args.boot)


def mode_frac(args):
    pols = [HEADLINE[0]] + [
        (f"scale {int(f*100)}%@5 + run", P(tp=40, trail=15, activate=20, tiers=[(5, f)]))
        for f in (0.25, 0.33, 0.5, 0.66, 0.75)
    ]
    for uname, u in UNIVERSES:
        e = get_entries(u, args.gap, args.vol_mult, args.hold, args.dedupe)
        run_table(f"TRIM-FRACTION GRID — {uname}", e, args.hold, pols, args.cost_bps, args.boot)


def mode_ladder(args):
    pols = [HEADLINE[0]] + [
        (f"33%@{a},33%@{b} + run", P(tp=40, trail=15, activate=20, tiers=[(a, 0.33), (b, 0.33)]))
        for a in (4, 5, 6) for b in (8, 10, 12)
    ]
    for uname, u in UNIVERSES:
        e = get_entries(u, args.gap, args.vol_mult, args.hold, args.dedupe)
        run_table(f"TWO-TIER LADDER GRID — {uname}", e, args.hold, pols, args.cost_bps, args.boot)


def mode_hold(args):
    for uname, u in UNIVERSES:
        print(f"\n--- HOLD SENSITIVITY — {uname} (entry set re-found per hold) ---")
        print(f"  {'policy':<30}{'hold':>5}{'n':>6}{'mean':>8}{'median':>8}{'win%':>6}{'gavebk':>8}{'days':>6}")
        for h in (5, 8, 10, 15, 20):
            e = get_entries(u, args.gap, args.vol_mult, h, args.dedupe)
            for name, pol in HEADLINE:
                r = qw.evalpol(e, h, {**pol, "cost_bps": args.cost_bps})
                print(f"  {name:<30}{h:>5}{r['n']:>6}{r['mean']*100:>+7.2f}%{r['median']*100:>+7.2f}%"
                      f"{r['win']*100:>5.0f}%{r['gaveback']*100:>7.0f}%{r['days']:>6.1f}")
            print()


def mode_entry(args):
    for uname, u in UNIVERSES:
        print(f"\n--- ENTRY SENSITIVITY — {uname} (gap% x vol-mult; hold={args.hold}) ---")
        print(f"  {'gap':>4}{'vol':>5}{'n':>6} | {'LET-RUN mean':>13}{'med':>7} | {'scale50@5 mean':>15}{'med':>7}{'win%':>6}{'gavebk':>8}")
        for g in (5.0, 7.0, 10.0):
            for v in (1.5, 2.0, 3.0):
                e = get_entries(u, g, v, args.hold, args.dedupe)
                if len(e) < 30:
                    print(f"  {g:>4.0f}{v:>5.1f}{len(e):>6} | (too few entries)")
                    continue
                a = qw.evalpol(e, args.hold, {**HEADLINE[0][1], "cost_bps": args.cost_bps})
                b = qw.evalpol(e, args.hold, {**HEADLINE[2][1], "cost_bps": args.cost_bps})
                print(f"  {g:>4.0f}{v:>5.1f}{len(e):>6} | {a['mean']*100:>+12.2f}%{a['median']*100:>+6.2f}% |"
                      f" {b['mean']*100:>+14.2f}%{b['median']*100:>+6.2f}%{b['win']*100:>5.0f}%{b['gaveback']*100:>7.0f}%")


def mode_cost(args):
    for uname, u in UNIVERSES:
        e = get_entries(u, args.gap, args.vol_mult, args.hold, args.dedupe)
        for c in (10.0, 15.0, 25.0, 40.0):
            run_table(f"COST {c:.0f}bps/leg — {uname}", e, args.hold, HEADLINE, c)


def lot_tiers(pol, entry_px, pos_usd):
    """Quantize a policy's fractional tiers to the whole-share trims live would actually place:
    shares0 = floor(pos_usd/px); trim = round(frac*shares0) capped at remaining-1 (the remnant must
    stay >=1 whole share to keep its resting stop); a trim that rounds to 0 is SKIPPED. Returns the
    achievable-fraction tier list (entries the cap can't buy 1 share of return None)."""
    shares0 = int(pos_usd // entry_px)
    if shares0 < 1:
        return None
    tiers, remaining = [], shares0
    for g, f in sorted(pol.get("tiers") or []):
        trim = int(f * shares0 + 0.5)
        trim = min(trim, remaining - 1)
        if trim < 1:
            continue
        tiers.append((g, trim / shares0))
        remaining -= trim
    return tiers


def mode_lot(args):
    for uname, u in UNIVERSES:
        e = get_entries(u, args.gap, args.vol_mult, args.hold, args.dedupe)
        for pos_usd in (310.0, 1000.0):
            print(f"\n--- WHOLE-SHARE LOTS @ ${pos_usd:.0f}/position — {uname} ---")
            print(HDR + "   skipped(no 1 share)")
            for name, pol in HEADLINE:
                rets, days, peaks, skipped = [], [], [], 0
                for _, bars, i, entry in e:
                    t = lot_tiers(pol, entry, pos_usd)
                    if t is None:
                        skipped += 1
                        continue
                    r, d, pk = qw.simulate(bars, i, entry, args.hold,
                                           {**pol, "tiers": t, "cost_bps": args.cost_bps})
                    rets.append(r); days.append(d); peaks.append(pk)
                n = len(rets)
                if not n:
                    continue
                m, sd = gd.stats(rets)
                gb = sum(1 for r, pk in zip(rets, peaks) if pk >= 5.0 and r < 0.02) / n
                r = {"mean": m, "median": gd.median(rets), "win": sum(1 for x in rets if x > 0) / n,
                     "sharpe": (m / sd) if sd else 0.0, "p10": qw.quantile(rets, 0.10),
                     "gaveback": gb, "days": sum(days) / n}
                print(fmt(name, r, f"   {skipped}"))


def mode_protect(args):
    """Re-tune the PROTECTION layer (stop / softcut / be) under each exit family — the current
    stop12/softcut8/be12 was tuned on the pre-scale-out config."""
    exits = [("LET-RUN", {"tp": 40, "trail": 15, "activate": 20}),
             ("scale 50%@8 + tr12@12", {"tp": 40, "trail": 12, "activate": 12, "tiers": [(8, 0.5)]})]
    for uname, u in UNIVERSES:
        e = get_entries(u, args.gap, args.vol_mult, args.hold, args.dedupe)
        for ename, ex in exits:
            pols = []
            for stop in (8, 12, 16, None):
                pols.append((f"stop{stop or 'OFF'} sc8 be12", {**ex, "stop": stop, "softcut": 8, "be": 12}))
            for sc in (6, 10, None):
                pols.append((f"stop12 sc{sc or 'OFF'} be12", {**ex, "stop": 12, "softcut": sc, "be": 12}))
            for be in (10, 15, 20, None):
                pols.append((f"stop12 sc8 be{be or 'OFF'}", {**ex, "stop": 12, "softcut": 8, "be": be}))
            run_table(f"PROTECTION GRID under {ename} — {uname}", e, args.hold, pols, args.cost_bps)


def mode_quality(args):
    """Entry-QUALITY conditioning: does the gap-day candle predict which entries are worth taking?
    (a) close position in the day's range (strong close = drift confirmation — classic PEAD),
    (b) gap size bucket. Both are observable at entry time — usable as live entry filters."""
    for uname, u in UNIVERSES:
        e = get_entries(u, args.gap, args.vol_mult, args.hold, args.dedupe)
        strong, weak = [], []
        for x in e:
            b = x[1][x[2]]
            rng = b["high"] - b["low"]
            (strong if (rng > 0 and (b["close"] - b["low"]) / rng >= 0.5) else weak).append(x)
        for label, bucket in (("STRONG CLOSE (top half of range)", strong),
                              ("WEAK CLOSE (bottom half)", weak)):
            if len(bucket) >= 30:
                run_table(f"{label} — {uname}", bucket, args.hold, HEADLINE, args.cost_bps)
        buckets = {"gap 7-10%": [], "gap 10-15%": [], "gap 15%+": []}
        for x in e:
            b, prev = x[1][x[2]], x[1][x[2] - 1]
            g = (b["open"] / prev["close"] - 1) * 100
            key = "gap 7-10%" if g < 10 else ("gap 10-15%" if g < 15 else "gap 15%+")
            buckets[key].append(x)
        for label, bucket in buckets.items():
            if len(bucket) >= 30:
                run_table(f"{label} — {uname}", bucket, args.hold, HEADLINE, args.cost_bps)


def mode_portfolio(args):
    """Capital-constrained PORTFOLIO sim — the account-level question the per-trade tables miss.
    K slots (default 6 ≈ 90% exposure / 15% position), each entry takes equity/K; an exit frees its
    slot the same day. A shorter-hold exit recycles capital into MORE trades, so a lower per-trade
    mean can still compound faster. Single path (no pairing) — read big gaps only."""
    K = args.slots
    for uname, u in UNIVERSES:
        e = get_entries(u, args.gap, args.vol_mult, args.hold, args.dedupe)
        print(f"\n--- PORTFOLIO SIM — {uname} ({len(e)} signals, {K} slots, equity/K per entry) ---")
        print(f"  {'policy':<30}{'taken':>6}{'final equity x':>15}{'maxDD':>8}{'avg hold':>9}")
        for name, pol in HEADLINE:
            sims = []
            for _, bars, i, entry in e:
                r, d, _ = qw.simulate(bars, i, entry, args.hold, {**pol, "cost_bps": args.cost_bps})
                sims.append((bars[i]["date"], bars[i + d]["date"], r, d))
            sims.sort()
            equity, busy, taken, holds = 1.0, [], 0, []
            peak, maxdd = 1.0, 0.0
            for ed, xd, r, d in sims:
                still = []                       # settle exits due on/before this entry date
                for bxd, alloc, br in busy:
                    if bxd <= ed:
                        equity += alloc * br
                        peak = max(peak, equity)
                        maxdd = max(maxdd, 1 - equity / peak)
                    else:
                        still.append((bxd, alloc, br))
                busy = still
                if len(busy) < K:
                    busy.append((xd, equity / K, r))
                    taken += 1
                    holds.append(d)
            for _, alloc, br in busy:           # settle whatever is still open at the end
                equity += alloc * br
            peak = max(peak, equity)
            maxdd = max(maxdd, 1 - equity / peak)
            avgh = sum(holds) / len(holds) if holds else 0.0
            print(f"  {name:<30}{taken:>6}{equity:>14.2f}x{maxdd*100:>7.1f}%{avgh:>9.1f}")


def find_mover_entries(universe, day_pct, hold, kind="all", refresh=False):
    """DISCO-style entries: the daily-MOVERS screen, not the PEAD gap. Discovery surfaces the day's
    top gainers (close/prev_close), with NO gap or rel-volume requirement — including pure intraday
    runners that never gapped. Enter at the mover-day close (the engine buys movers intraday/at
    close). kind: all | gapless (gap<2% — the runner discovery catches but PEAD never would) |
    gappy (gap>=2% — overlaps the PEAD cohort)."""
    entries = []
    for sym in universe:
        bars = gd.clean_bars(gd.load_bars(sym, refresh))
        if len(bars) < 30 + hold:
            continue
        for i in range(21, len(bars) - hold):
            b, prev = bars[i], bars[i - 1]
            if abs(bars[i + hold]["close"] / b["close"] - 1) > 3.0:
                continue
            if (b["close"] / prev["close"] - 1) * 100 < day_pct:
                continue
            gap = (b["open"] / prev["close"] - 1) * 100
            if kind == "gapless" and gap >= 2.0:
                continue
            if kind == "gappy" and gap < 2.0:
                continue
            entries.append((sym, bars, i, b["close"]))
    return entries


def mode_disco(args):
    """The risky-mode entry test: do the exit verdicts hold when the ENTRY is the disco movers
    screen (close-to-close gainers, no gap/volume gate) instead of the PEAD gap?"""
    for uname, u in UNIVERSES:
        for kind, label in (("all", f"ALL MOVERS >= {args.gap:.0f}% c/c"),
                            ("gapless", "GAPLESS MOVERS (intraday runners, gap<2%)"),
                            ("gappy", "GAPPY MOVERS (gap>=2%)")):
            e = find_mover_entries(u, args.gap, args.hold, kind)
            if args.dedupe:
                e = qw.dedupe_entries(e, args.hold)
            if len(e) < 30:
                print(f"\n--- DISCO {label} — {uname}: only {len(e)} entries, skipping ---")
                continue
            run_table(f"DISCO {label} — {uname}", e, args.hold, HEADLINE, args.cost_bps, args.boot)


SLOT_POLICIES = [
    ("LET-RUN tp40 tr15@20",   P(tp=40, trail=15, activate=20)),
    ("tight TP8",              P(tp=8)),
    ("tight TP10",             P(tp=10)),
    ("tight TP12",             P(tp=12)),
    ("tight TP15",             P(tp=15)),
    ("tight TP20",             P(tp=20)),
    ("scale 50%@8 + tr12@12",  P(tp=40, trail=12, activate=12, tiers=[(8, 0.5)])),
    ("scale 50%@10 + run",     P(tp=40, trail=15, activate=20, tiers=[(10, 0.5)])),
    ("ladder 33@6,33@10",      P(tp=40, trail=15, activate=20, tiers=[(6, 0.33), (10, 0.33)])),
    ("ladder 33@8,33@12",      P(tp=40, trail=15, activate=20, tiers=[(8, 0.33), (12, 0.33)])),
]


def run_portfolio(entries, hold, pol, cost, K):
    """One capital-constrained path. Returns (taken, n_signals, final_equity, maxDD, avg_hold)."""
    sims = []
    for _, bars, i, entry in entries:
        r, d, _ = qw.simulate(bars, i, entry, hold, {**pol, "cost_bps": cost})
        sims.append((bars[i]["date"], bars[i + d]["date"], r, d))
    sims.sort()
    equity, busy, taken, holds = 1.0, [], 0, []
    peak, maxdd = 1.0, 0.0
    for ed, xd, r, d in sims:
        still = []
        for bxd, alloc, br in busy:
            if bxd <= ed:
                equity += alloc * br
                peak = max(peak, equity)
                maxdd = max(maxdd, 1 - equity / peak)
            else:
                still.append((bxd, alloc, br))
        busy = still
        if len(busy) < K:
            busy.append((xd, equity / K, r))
            taken += 1
            holds.append(d)
    for _, alloc, br in busy:
        equity += alloc * br
    peak = max(peak, equity)
    maxdd = max(maxdd, 1 - equity / peak)
    return taken, len(sims), equity, maxdd, (sum(holds) / len(holds) if holds else 0.0)


def mode_slots(args):
    """Slot-count sensitivity of the portfolio ranking: does TP-recycling dominance survive when
    the book can hold 10/20/30 names? COMBINED = LARGE+MIDCAP movers merged (disco trades anything
    that moves). Equal-size equity/K per entry; utilization = taken/signals."""
    streams = []
    for uname, u in UNIVERSES:
        streams.append((uname, get_entries(u, args.gap, args.vol_mult, args.hold, args.dedupe)))
    streams.append(("COMBINED (LARGE+MIDCAP)", streams[0][1] + streams[1][1]))
    for uname, e in streams:
        print(f"\n--- SLOT SWEEP — {uname} movers/{ENTRY_STYLE} stream ({len(e)} signals) ---")
        print(f"  {'policy':<26}" + "".join(f"{'K='+str(k):>16}" for k in (4, 6, 10, 15, 20, 30)))
        for name, pol in SLOT_POLICIES:
            cells = []
            for K in (4, 6, 10, 15, 20, 30):
                taken, n, eq, dd, _ = run_portfolio(e, args.hold, pol, args.cost_bps, K)
                cells.append(f"{eq:>7.2f}x {taken*100//n:>3}%u")
            print(f"  {name:<26}" + "".join(f"{c:>16}" for c in cells))
        print("  (cell = terminal equity multiple, %u = signals taken / signals available)")


def spy_regime():
    """date -> True (risk_on: SPY close > 50d MA) from the keyless Cboe SPY history."""
    bars = gd.clean_bars(gd.load_bars("SPY"))
    out = {}
    closes = [b["close"] for b in bars]
    for i, b in enumerate(bars):
        if i >= 50:
            ma = sum(closes[i - 50:i]) / 50.0
            out[b["date"]] = b["close"] > ma
    return out


def mode_regime(args):
    reg = spy_regime()
    for uname, u in UNIVERSES:
        e = get_entries(u, args.gap, args.vol_mult, args.hold, args.dedupe)
        on = [x for x in e if reg.get(x[1][x[2]]["date"]) is True]
        off = [x for x in e if reg.get(x[1][x[2]]["date"]) is False]
        for label, bucket in (("RISK-ON (SPY>50dMA)", on), ("RISK-OFF (SPY<50dMA)", off)):
            if len(bucket) < 30:
                print(f"\n--- {label} — {uname}: only {len(bucket)} entries, skipping ---")
                continue
            run_table(f"{label} — {uname}", bucket, args.hold, HEADLINE, args.cost_bps)


def mode_year(args):
    for uname, u in UNIVERSES:
        e = get_entries(u, args.gap, args.vol_mult, args.hold, args.dedupe)
        byyear = {}
        for x in e:
            byyear.setdefault(x[1][x[2]]["date"][:4], []).append(x)
        print(f"\n--- YEAR-BY-YEAR — {uname} (LET-RUN vs scale 50%@5; hold={args.hold}) ---")
        print(f"  {'year':<6}{'n':>5} | {'LR mean':>9}{'LR med':>8} | {'SC mean':>9}{'SC med':>8}{'SC win%':>8}")
        for y in sorted(byyear):
            b = byyear[y]
            if len(b) < 10:
                continue
            a = qw.evalpol(b, args.hold, {**HEADLINE[0][1], "cost_bps": args.cost_bps})
            s = qw.evalpol(b, args.hold, {**HEADLINE[2][1], "cost_bps": args.cost_bps})
            print(f"  {y:<6}{len(b):>5} | {a['mean']*100:>+8.2f}%{a['median']*100:>+7.2f}% |"
                  f" {s['mean']*100:>+8.2f}%{s['median']*100:>+7.2f}%{s['win']*100:>7.0f}%")


MODES = {"tiergain": mode_tiergain, "frac": mode_frac, "ladder": mode_ladder, "hold": mode_hold,
         "entry": mode_entry, "cost": mode_cost, "lot": mode_lot, "regime": mode_regime,
         "year": mode_year, "protect": mode_protect, "quality": mode_quality,
         "portfolio": mode_portfolio, "disco": mode_disco, "slots": mode_slots}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=list(MODES) + ["all"], default="all")
    ap.add_argument("--gap", type=float, default=7.0)
    ap.add_argument("--vol-mult", type=float, default=2.0)
    ap.add_argument("--hold", type=int, default=15)
    ap.add_argument("--cost-bps", type=float, default=15.0)
    ap.add_argument("--boot", type=int, default=0)
    ap.add_argument("--slots", type=int, default=6,
                    help="portfolio mode: concurrent position slots (~exposure cap / position cap)")
    ap.add_argument("--entry", choices=["gap", "movers"], default="gap",
                    help="entry screen: gap = PEAD gap+vol; movers = disco daily-gainers (c/c, no gates)")
    ap.add_argument("--overlap", dest="dedupe", action="store_false",
                    help="keep overlapping same-symbol entries (the pre-2026-06-10 behaviour)")
    args = ap.parse_args()

    global ENTRY_STYLE
    ENTRY_STYLE = args.entry
    print(f"ENTRY: {args.entry} >={args.gap}% (vol>={args.vol_mult}x if gap) | hold={args.hold}d | "
          f"cost {args.cost_bps}bps/leg | entries {'DEDUPED' if args.dedupe else 'OVERLAPPING'}")
    print("Protection on all policies: stop12 / softcut8 / be12. LARGE=trustworthy; MIDCAP=BIASED (direction only).")
    for m in (MODES if args.mode == "all" else {args.mode: MODES[args.mode]}):
        print("\n" + "=" * 104)
        print(f"MODE: {m.upper()}")
        print("=" * 104)
        MODES[m](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
