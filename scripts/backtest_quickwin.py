#!/usr/bin/env python3
"""
backtest_quickwin.py — does a QUICK-WIN exit profile (tight TP / scale-out) serve the owner's
goals (downside protection, quick wins, a little money along the way) better than the LET-RUN
profile the drift edge wants?

Reuses the entry set + loaders from backtest_exit_policy / backtest_gap_drift (the gap-drift
catalyst entry), and adds what that harness lacks: TIERED SCALE-OUT exits (sell a fraction at +X%,
ride the rest). Reports the metrics that map to the owner's objective, not just mean:
  - mean / median        — central outcome (quick-win trades the mean down, median up)
  - win%                 — frequency of a green close (the "quick wins" goal)
  - p10                  — 10th-percentile return = left tail (the "downside protection" goal)
  - gaveback%            — trades whose PEAK reached >=+5% but CLOSED < +2% (a quick win left on
                           the table; the exact "we were up then gave it back" complaint)
  - avg_d                — average days held (quick-win should be shorter)

CAVEAT (read it): this is the gap-drift universe (LARGE = mega-cap; MIDCAP = survivorship+recency
biased), NOT the live `disco` small/mid discretionary tape. It measures how a quick-win exit behaves
on the MECHANISM (catalyst names), which is informative but not a disco verdict. Disco must still be
earned forward in paper. Daily OHLC; stop ratchets off prior-day high-water; net of cost per leg.

Usage:
  python3 scripts/backtest_quickwin.py                 # LARGE + MIDCAP, gap7/vol2, 15d
  python3 scripts/backtest_quickwin.py --gap 7 --hold 15
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_gap_drift as gd
import backtest_exit_policy as bx


def simulate(bars, i, entry, hold, pol):
    """One trade under an exit policy that may include tiered scale-outs.

    pol keys: stop, tp (final-lot TP), trail, activate, be, softcut, tiers (list of (gain%, frac_of_
    original)), cost_bps. Returns (net_return_on_full_position, exit_day_of_last_share, peak_gain_pct).
    The protective rungs (stop/softcut/be/trail) govern the REMAINING shares; each exit leg pays cost.
    """
    cat = pol.get("stop")
    tp = pol.get("tp")
    trail = pol.get("trail") or 0.0
    act = pol.get("activate", 0.0)
    be = pol.get("be")
    softcut = pol.get("softcut")
    tiers = sorted(pol.get("tiers") or [], key=lambda t: t[0])   # ascending by gain%
    C = pol.get("cost_bps", 15.0)

    stop_px = entry * (1 - cat / 100.0) if cat else None
    tp_px = entry * (1 + tp / 100.0) if tp else None
    floor = stop_px if stop_px is not None else -1e18
    hw = entry
    remaining = 1.0
    realized = 0.0          # Σ frac_sold * (exit_px/entry - 1), gross
    legs = 1.0              # cost legs: entry counts as 1; each sell-fraction adds its fraction
    fired = set()           # tier indices already taken
    exit_day = hold
    peak = 0.0

    def sell(frac, px, day):
        nonlocal remaining, realized, legs, exit_day, peak
        frac = min(frac, remaining)
        if frac <= 1e-9:
            return
        realized += frac * (px / entry - 1.0)
        legs += frac
        remaining -= frac
        exit_day = day
        # a fill at px proves the price traded there — count it toward the peak (gaveback metric);
        # day-highs of held days still only count after the day completes (no adverse-order peeking)
        peak = max(peak, (px / entry - 1.0) * 100.0)

    for k in range(1, hold + 1):
        o, h, l = bars[i + k]["open"], bars[i + k]["high"], bars[i + k]["low"]
        # --- protective exits on the REMAINING shares (conservative: adverse print first) ---
        if remaining > 0 and stop_px is not None and o <= stop_px:
            sell(remaining, o, k); break
        if remaining > 0 and stop_px is not None and l <= stop_px:
            sell(remaining, stop_px, k); break
        # --- scale-out tiers (whole-of-fraction at the tier price; gap-through fills at open) ---
        for ti, (g, frac) in enumerate(tiers):
            if ti in fired or remaining <= 0:
                continue
            tier_px = entry * (1 + g / 100.0)
            if h >= tier_px:
                fired.add(ti)
                sell(frac, max(tier_px, o) if o > tier_px else tier_px, k)
        # --- final-lot TP on whatever remains ---
        if remaining > 0 and tp_px is not None and h >= tp_px:
            sell(remaining, max(tp_px, o) if o > tp_px else tp_px, k); break
        # --- soft-cut at the close (deep & falling) ---
        c = bars[i + k]["close"]
        if remaining > 0 and softcut is not None and c <= entry * (1 - softcut / 100.0) and c < o:
            sell(remaining, c, k); break
        # --- ratchet the protective stop for tomorrow off the high-water ---
        if h > hw:
            hw = h
        peak = max(peak, (hw / entry - 1.0) * 100.0)
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

    if remaining > 1e-9:                       # time-exit the rest
        sell(remaining, bars[i + hold]["close"], hold)
    net = realized - (legs * C / 10000.0)      # cost per leg (entry + each sell fraction)
    return net, exit_day, peak


def dedupe_entries(entries, hold):
    """Drop entries that overlap a still-open prior trade in the same symbol (live can't re-enter a
    held name — COOLDOWN + already-holding). Keeps the FIRST entry of each event cluster."""
    out, last = [], {}
    for sym, bars, i, entry in entries:           # find_entries yields per-symbol ascending i
        if sym in last and i - last[sym] <= hold:
            continue
        last[sym] = i
        out.append((sym, bars, i, entry))
    return out


def quantile(xs, q):
    if not xs:
        return 0.0
    s = sorted(xs)
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def evalpol(entries, hold, pol, want_rets=False):
    rets, days, peaks = [], [], []
    for _, bars, i, entry in entries:
        r, d, pk = simulate(bars, i, entry, hold, pol)
        rets.append(r); days.append(d); peaks.append(pk)
    n = len(rets)
    m, sd = gd.stats(rets)
    win = sum(1 for x in rets if x > 0) / n if n else 0.0
    # gaveback: peaked >=+5% but closed < +2% net
    gb = sum(1 for r, pk in zip(rets, peaks) if pk >= 5.0 and r < 0.02)
    gb_frac = gb / n if n else 0.0
    out = {"mean": m, "median": gd.median(rets), "win": win, "sharpe": (m / sd) if sd else 0.0,
           "p10": quantile(rets, 0.10), "gaveback": gb_frac,
           "days": sum(days) / n if n else 0.0, "n": n}
    if want_rets:
        out["rets"] = rets
    return out


def boot_ci(ref_rets, pol_rets, nboot=2000, seed=42):
    """Paired bootstrap CI (5–95%) on the mean and median DIFFERENCE pol − ref over the same entries.
    Pairing strips the shared entry-set noise so only the policy delta remains."""
    import random
    n = len(ref_rets)
    if n == 0 or len(pol_rets) != n:
        return (0.0, 0.0), (0.0, 0.0)
    diffs = [p - r for p, r in zip(pol_rets, ref_rets)]
    rng = random.Random(seed)
    mdiffs, meddiffs = [], []
    for _ in range(nboot):
        sample = [diffs[rng.randrange(n)] for _ in range(n)]
        mdiffs.append(sum(sample) / n)
        meddiffs.append(gd.median(sample))
    return ((quantile(mdiffs, 0.05), quantile(mdiffs, 0.95)),
            (quantile(meddiffs, 0.05), quantile(meddiffs, 0.95)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap", type=float, default=7.0)
    ap.add_argument("--vol-mult", type=float, default=2.0)
    ap.add_argument("--hold", type=int, default=15)
    ap.add_argument("--cost-bps", type=float, default=15.0)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--dedupe", action="store_true",
                    help="drop entries overlapping a still-open same-symbol trade (live realism)")
    ap.add_argument("--boot", type=int, default=0, metavar="N",
                    help="N paired-bootstrap resamples -> 5-95%% CI of each policy's mean diff vs row 1")
    args = ap.parse_args()
    C = args.cost_bps

    # Shared protection layer on every policy (stop12 / softcut8 / be12) — the owner's goal #1.
    base = {"stop": 12, "softcut": 8, "be": 12, "cost_bps": C}
    def P(**kw):
        d = dict(base); d.update(kw); return d

    POLICIES = [
        ("LET-RUN (pead: tp40 tr15@20)", P(tp=40, trail=15, activate=20)),
        ("tight TP8",                    P(tp=8)),
        ("tight TP10",                   P(tp=10)),
        ("tight TP15",                   P(tp=15)),
        ("scale 50%@5 + run(tp40 tr15)", P(tp=40, trail=15, activate=20, tiers=[(5, 0.5)])),
        ("scale 33%@5,33%@8 + run",      P(tp=40, trail=15, activate=20, tiers=[(5, 0.33), (8, 0.33)])),
        ("scale 33%@5,33%@10 + trail",   P(tp=40, trail=15, activate=20, tiers=[(5, 0.33), (10, 0.33)])),
        ("scale 50%@8 + trail rest",     P(tp=40, trail=12, activate=12, tiers=[(8, 0.5)])),
    ]

    for uname, u in [("LARGE (mega-cap, trustworthy)", gd.LARGE),
                     ("MIDCAP (closer-to-disco, BIASED)", gd.MIDCAP)]:
        entries = bx.find_entries(u, args.gap, args.vol_mult, args.hold, args.refresh)
        if args.dedupe:
            entries = dedupe_entries(entries, args.hold)
        w = 100
        print("=" * w)
        print(f"QUICK-WIN EXIT SWEEP — {uname} | gap>={args.gap}% vol>={args.vol_mult}x | "
              f"hold={args.hold}d | {len(entries)} entries{' (deduped)' if args.dedupe else ''} | "
              f"cost {C}bps/leg")
        print("  shared protection on ALL: stop12 / softcut8 / be12.  Goals: win%↑ p10↑(less neg) gaveback↓")
        print("=" * w)
        hdr = (f"  {'policy':<32}{'mean':>7}{'median':>8}{'win%':>6}{'sharpe':>7}{'p10':>8}{'gavebk':>8}{'days':>6}")
        print(hdr + ("   mean-diff 5-95% CI" if args.boot else ""))
        print("  " + "-" * (w - 2))
        ref_rets = None
        for name, pol in POLICIES:
            r = evalpol(entries, args.hold, pol, want_rets=bool(args.boot))
            line = (f"  {name:<32}{r['mean']*100:>+6.2f}%{r['median']*100:>+7.2f}%{r['win']*100:>5.0f}%"
                    f"{r['sharpe']:>7.3f}{r['p10']*100:>+7.1f}%{r['gaveback']*100:>7.0f}%{r['days']:>6.1f}")
            if args.boot:
                if ref_rets is None:
                    ref_rets = r["rets"]
                    line += "   (reference)"
                else:
                    (mlo, mhi), _ = boot_ci(ref_rets, r["rets"], args.boot)
                    sig = "*" if (mlo > 0 or mhi < 0) else " "
                    line += f"   [{mlo*100:+.2f}%, {mhi*100:+.2f}%]{sig}"
            print(line)
        print()
    if args.boot:
        print("CI: paired bootstrap of (policy - row1) per-entry return diffs; * = excludes 0.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
