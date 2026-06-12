#!/usr/bin/env python3
"""
backtest_exit_policy.py — which EXIT policy best harvests the catalyst gap-drift edge?

backtest_gap_drift.py established the entry edge (overnight gap + volume -> multi-day PEAD drift,
trustworthy on LARGE caps). This script holds that ENTRY fixed and sweeps the EXIT side — the part
the live engine actually controls: the catastrophe stop, a breakeven ratchet, a continuous trailing
stop that scales up with price, an optional take-profit, and the time-exit (drift window).

It mirrors the LIVE stop schedule in live_execute.trail_stop_price():
  - catastrophe stop  = entry x (1 - STOP%)               (static floor)
  - breakeven rung    = lift stop to entry once up BE%    (TRAIL_BREAKEVEN_AT_PCT)
  - trailing rung     = HW x (1 - TRAIL%) once up ACT%    (TRAIL_STOP_PCT / TRAIL_ACTIVATE_PCT)
  - take-profit       = full exit at +TP%                 (TAKE_PROFIT_PCT)
  - time-exit         = close on day HOLD                 (MAX_HOLD_DAYS proxy)

Daily OHLC (keyless Cboe cache). The stop ratchets off the PRIOR days' high-water mark — the
daily-bar analog of the engine's discrete-cadence trailing (no peeking at today's high to tighten
today's stop). Within a day the order is conservative: gap-at-open, then intraday STOP before
intraday TP (assume the adverse print first). Net of 2x cost-bps round-trip.

Usage:
  python3 scripts/backtest_exit_policy.py                      # LARGE, gap7/vol2, 15d hold
  python3 scripts/backtest_exit_policy.py --universe MIDCAP --gap 7 --hold 20
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_gap_drift as gd  # reuse loaders, cleaners, universes, stats


def find_entries(universe, gap_pct, vol_mult, hold, refresh):
    """All gap-day entries in a universe: (symbol, bars, i, entry_close). Entry at gap-day close."""
    entries = []
    for sym in universe:
        bars = gd.clean_bars(gd.load_bars(sym, refresh))
        if len(bars) < 30 + hold:
            continue
        vols = [b["volume"] for b in bars]
        for i in range(21, len(bars) - hold):
            b, prev = bars[i], bars[i - 1]
            if abs(bars[i + hold]["close"] / b["close"] - 1) > 3.0:  # residual-glitch guard
                continue
            avgv = sum(vols[i - 20:i]) / 20.0
            if avgv <= 0:
                continue
            gap = b["open"] / prev["close"] - 1
            if gap * 100 < gap_pct or b["volume"] / avgv < vol_mult:
                continue
            entries.append((sym, bars, i, b["close"]))
    return entries


def simulate(bars, i, entry, hold, pol):
    """One trade's net return + exit day under an exit policy. Returns (ret, exit_day)."""
    cat = pol.get("stop")            # catastrophe stop % (None = no stop)
    tp = pol.get("tp")               # take-profit %      (None = off)
    trail = pol.get("trail") or 0.0  # trail % below high-water (0 = off)
    act = pol.get("activate", 0.0)   # trail activation gain %
    be = pol.get("be")               # breakeven trigger % (None = off)
    softcut = pol.get("softcut")     # Tier-1 soft-cut proxy (hold_risk.py): exit at the CLOSE of a
                                     #   DOWN day this % underwater ("down past soft-cut & falling")
    crit = pol.get("crit_frac")      # Tier-1 critical proxy: exit at the close once this fraction
                                     #   of the way down to the catastrophe stop (risk>=70 band)
    cost_bps = pol.get("cost_bps", 15.0)

    stop_px = entry * (1 - cat / 100.0) if cat else None
    tp_px = entry * (1 + tp / 100.0) if tp else None
    floor = stop_px if stop_px is not None else -1e18
    hw = entry
    ret, exit_day = None, hold
    for k in range(1, hold + 1):
        o, h, l = bars[i + k]["open"], bars[i + k]["high"], bars[i + k]["low"]
        if stop_px is not None and o <= stop_px:          # gap down through the stop
            ret, exit_day = o / entry - 1, k; break
        if tp_px is not None and o >= tp_px:              # gap up through the TP
            ret, exit_day = tp_px / entry - 1, k; break
        if stop_px is not None and l <= stop_px:          # intraday stop (conservative: before TP)
            ret, exit_day = stop_px / entry - 1, k; break
        if tp_px is not None and h >= tp_px:              # intraday TP
            ret, exit_day = tp_px / entry - 1, k; break
        # Tier-1 protective-sell proxies, evaluated at the CLOSE (no intraday peeking): the live
        # monitor sells a loser that is BOTH deep underwater and still falling (soft-cut), or that
        # has burned most of the runway to the catastrophe stop (critical).
        c = bars[i + k]["close"]
        if softcut is not None and c <= entry * (1 - softcut / 100.0) and c < o:
            ret, exit_day = c / entry - 1, k; break
        if crit is not None and cat and c <= entry * (1 - crit * cat / 100.0):
            ret, exit_day = c / entry - 1, k; break
        # update high-water from today's high -> ratchet the stop for TOMORROW
        if h > hw:
            hw = h
        gain = (hw / entry - 1) * 100.0
        cands = [floor]
        if be is not None and be > 0 and gain >= be:
            cands.append(entry * (1 + pol.get("be_off", 0.0) / 100.0))  # be_off: lift above entry (live parity)
        if trail > 0 and gain >= act:
            cands.append(hw * (1 - trail / 100.0))
        nxt = max(cands)
        if stop_px is None or nxt > stop_px:
            stop_px = nxt
    if ret is None:
        ret = bars[i + hold]["close"] / entry - 1            # time-exit
    return ret - 2 * cost_bps / 10000.0, exit_day


def evalpol(entries, hold, pol):
    rets, days = [], []
    for _, bars, i, entry in entries:
        r, d = simulate(bars, i, entry, hold, pol)
        rets.append(r)
        days.append(d)
    m, sd = gd.stats(rets)
    med = gd.median(rets)
    wr = sum(1 for x in rets if x > 0) / len(rets) if rets else 0.0
    sharpe = (m / sd) if sd > 0 else 0.0          # per-trade (not annualized)
    avgd = sum(days) / len(days) if days else 0.0
    return {"mean": m, "median": med, "sd": sd, "sharpe": sharpe, "win": wr, "days": avgd, "n": len(rets)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", choices=["LARGE", "MIDCAP", "BOTH"], default="LARGE")
    ap.add_argument("--gap", type=float, default=7.0)
    ap.add_argument("--vol-mult", type=float, default=2.0)
    ap.add_argument("--hold", type=int, default=15, help="time-exit, trading days (~21 calendar)")
    ap.add_argument("--cost-bps", type=float, default=15.0)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    C = args.cost_bps
    # Curated policies: reference ceiling/floor, the current live config, breakeven, and the trail-width
    # curve (continuous, scaling up with price) with/without breakeven and activation.
    POLICIES = [
        ("TIME-ONLY (no stop/TP)",        {"stop": None, "tp": None}),
        ("BASELINE live (stop8, tp25)",   {"stop": 8, "tp": 25}),
        ("stop8 tp25 + be10",             {"stop": 8, "tp": 25, "be": 10}),
        ("trail20 act0 (loose)",          {"stop": 8, "tp": 25, "trail": 20, "activate": 0}),
        ("trail16 act0",                  {"stop": 8, "tp": 25, "trail": 16, "activate": 0}),
        ("trail12 act0",                  {"stop": 8, "tp": 25, "trail": 12, "activate": 0}),
        ("trail10 act0",                  {"stop": 8, "tp": 25, "trail": 10, "activate": 0}),
        ("trail8  act0 (tight)",          {"stop": 8, "tp": 25, "trail": 8,  "activate": 0}),
        ("trail16 act0 + be10",           {"stop": 8, "tp": 25, "trail": 16, "activate": 0, "be": 10}),
        ("trail12 act0 + be10",           {"stop": 8, "tp": 25, "trail": 12, "activate": 0, "be": 10}),
        ("trail12 act15 + be10 (late)",   {"stop": 8, "tp": 25, "trail": 12, "activate": 15, "be": 10}),
        ("trail12 act0 + be10, NO tp",    {"stop": 8, "tp": None, "trail": 12, "activate": 0, "be": 10}),
        # --- Tier-1 soft-cut audit (remediation plan P1): does the hold_risk.py protective sell
        # earn its keep on top of the CURRENT live exit config (stop12 / tp40 / trail15@20)?
        # softcutN = exit at the close of a down day N% underwater; crit65 = exit at the close once
        # 65% of the way down to the catastrophe stop (~-7.8% with stop12).
        ("LIVE cfg (stop12 tp40 tr15a20)", {"stop": 12, "tp": 40, "trail": 15, "activate": 20}),
        ("LIVE + be5  (owner idea)",       {"stop": 12, "tp": 40, "trail": 15, "activate": 20, "be": 5}),
        ("LIVE + be6",                     {"stop": 12, "tp": 40, "trail": 15, "activate": 20, "be": 6}),
        ("LIVE + be8",                     {"stop": 12, "tp": 40, "trail": 15, "activate": 20, "be": 8}),
        ("LIVE + be12",                    {"stop": 12, "tp": 40, "trail": 15, "activate": 20, "be": 12}),
        # giveback-cap: ratchet a stop up once GREEN (+6%), but to a small-loss floor (trail), not entry.
        ("giveback trail15 @act6",         {"stop": 12, "tp": 40, "trail": 15, "activate": 6}),
        ("giveback trail10 @act6",         {"stop": 12, "tp": 40, "trail": 10, "activate": 6}),
        ("giveback trail8  @act6",         {"stop": 12, "tp": 40, "trail": 8,  "activate": 6}),
        ("giveback trail10 @act6 + be12",  {"stop": 12, "tp": 40, "trail": 10, "activate": 6, "be": 12}),
        ("LIVE + be10",                    {"stop": 12, "tp": 40, "trail": 15, "activate": 20, "be": 10}),
        ("LIVE + softcut4",                {"stop": 12, "tp": 40, "trail": 15, "activate": 20, "softcut": 4}),
        ("LIVE + softcut6",                {"stop": 12, "tp": 40, "trail": 15, "activate": 20, "softcut": 6}),
        ("LIVE + softcut8",                {"stop": 12, "tp": 40, "trail": 15, "activate": 20, "softcut": 8}),
        ("LIVE + crit65",                  {"stop": 12, "tp": 40, "trail": 15, "activate": 20, "crit_frac": 0.65}),
        ("LIVE + softcut4 + crit65",       {"stop": 12, "tp": 40, "trail": 15, "activate": 20,
                                            "softcut": 4, "crit_frac": 0.65}),
    ]
    unis = [("LARGE", gd.LARGE), ("MIDCAP", gd.MIDCAP)] if args.universe == "BOTH" \
        else [(args.universe, gd.LARGE if args.universe == "LARGE" else gd.MIDCAP)]

    print(f"Loading history (cached if present)...", file=sys.stderr)
    for uname, u in unis:
        entries = find_entries(u, args.gap, args.vol_mult, args.hold, args.refresh)
        w = 96
        print("=" * w)
        print(f"EXIT-POLICY SWEEP — {uname} | gap>={args.gap}% vol>={args.vol_mult}x | "
              f"hold(time-exit)={args.hold}d | {len(entries)} entries | cost {C}bps/side")
        print("  (per-trade net returns over the hold; stop ratchets off prior-day high-water)")
        print("=" * w)
        print(f"  {'policy':<32} {'mean':>8} {'median':>8} {'win%':>6} {'sharpe':>7} {'avg_d':>6}")
        print("  " + "-" * (w - 2))
        rows = []
        for name, pol in POLICIES:
            rows.append((name, evalpol(entries, args.hold, {**pol, "cost_bps": C})))
        base = dict(rows)["BASELINE live (stop8, tp25)"]
        rows.sort(key=lambda r: r[1]["mean"], reverse=True)
        for name, s in rows:
            flag = "  <- current" if name.startswith("BASELINE") else \
                   ("  *best mean*" if name == rows[0][0] else "")
            print(f"  {name:<32} {s['mean']*100:>+7.2f}% {s['median']*100:>+7.2f}% "
                  f"{s['win']*100:>5.0f}% {s['sharpe']:>7.3f} {s['days']:>6.1f}{flag}")
        print("  " + "-" * (w - 2))
        bm = base["mean"] * 100
        print(f"  baseline mean = {bm:+.2f}%/trade. 'mean' is net per ~{args.hold}d hold; 'sharpe' is")
        print(f"  per-trade mean/sd (risk-adjusted). TIME-ONLY = the raw drift ceiling (no protection).")
    print("\nNOTE: LARGE is the trustworthy universe (MIDCAP is survivorship+recency biased).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
