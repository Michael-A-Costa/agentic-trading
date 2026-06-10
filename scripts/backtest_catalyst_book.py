#!/usr/bin/env python3
"""
backtest_catalyst_book.py — PORTFOLIO sim of the catalyst-drift book (the risky-sleeve mandate).

backtest_gap_drift.py proved the SIGNAL (overnight gap + volume spike drifts forward, LARGE-cap
t up to 3.10 at 10-20d) as an event study — per-event forward returns. That is NOT a tradable
number: it ignores capital constraints, concurrent positions, compounding, and drawdown. This script
turns the same signal into a TRADED, CONCENTRATED, MULTI-DAY book with a shared cash account and
reports the curve that actually decides deployment: CAGR, MaxDD, Sharpe, % time invested.

Models the owner-locked v1 mandate (strategies/catalyst-drift-v1-plan.md):
  - entry: gap = open/prev_close-1 >= --gap AND vol/avg20 >= --vol-mult  (catalyst proxy)
  - rank same-day signals by gap size; fill open slots up to --max-pos, CONCENTRATED (--pos-pct each)
  - WHOLE shares only (overnight holds need a real resting stop; fractional can't carry one)
  - exit: gap-aware stop (-{--stop}%), optional trailing stop, else time-exit at --hold days
  - costs: --cost-bps per side; --cooldown days before re-entering a name

=== HONESTY (read before trusting a number) ===
  - Survivorship + (MIDCAP) recency bias: both baskets are today's survivors. LARGE is the trustworthy
    floor; MIDCAP's absolute numbers are the LEAST trustworthy here and need live validation.
  - Daily bars: entry modeled at the gap-day CLOSE (no intraday fill), stop checked gap-aware on O/L.
  - **The backtest CANNOT model the agent's catalyst-confirmation / pump-filter** — the load-bearing
    alpha for the dirty small/mid signal. So this is a LOWER BOUND on a clean universe and UNRELIABLE
    (likely pessimistic) on MIDCAP, where filtering the pumps is exactly the agent's job.

Usage:
  python3 scripts/backtest_catalyst_book.py                      # BOTH baskets, defaults
  python3 scripts/backtest_catalyst_book.py --universe large
  python3 scripts/backtest_catalyst_book.py --gap 7 --hold 15 --stop 8 --max-pos 6 --pos-pct 0.15
  python3 scripts/backtest_catalyst_book.py --sweep              # gap x hold grid, key stats
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_gap_drift import LARGE, MIDCAP, load_bars, clean_bars  # noqa: E402

BENCH = "SPY"


def build(universe: list[str], refresh: bool) -> dict[str, dict]:
    """sym -> {dates: [...], bar: {date: barobj}, i: {date: idx}, vols: [...]}. Cleaned bars."""
    out = {}
    for sym in universe + [BENCH]:
        bars = clean_bars(load_bars(sym, refresh))
        if len(bars) < 40:
            continue
        dates = [b["date"] for b in bars]
        out[sym] = {
            "dates": dates,
            "bar": {b["date"]: b for b in bars},
            "i": {d: k for k, d in enumerate(dates)},
            "vols": [b["volume"] for b in bars],
            "bars": bars,
        }
    return out


def sim(data: dict[str, dict], syms: list[str], gap_pct: float, vol_mult: float, hold: int,
        stop: float, trail: float, max_pos: int, pos_pct: float, exposure_cap: float,
        cost_bps: float, cooldown: int, start_cash: float) -> dict:
    """Event-driven portfolio sim on the SPY trading calendar. Returns daily equity + trade log."""
    if BENCH not in data:
        return {}
    cal = data[BENCH]["dates"]                       # global clock = SPY trading days
    cost = cost_bps / 10000.0

    cash = start_cash
    positions: dict[str, dict] = {}                  # sym -> {shares, entry_px, stop_px, hi, t_entry}
    cooling: dict[str, int] = {}                     # sym -> calendar idx until which it's blocked
    equity_curve, invested_frac = [], []
    trades = []

    def mark(sym: str, d: str) -> float | None:
        b = data[sym]["bar"].get(d)
        return b["close"] if b else None

    for t, d in enumerate(cal):
        # ---- 1. manage / exit open positions on today's bar ----
        for sym in list(positions.keys()):
            p = positions[sym]
            b = data[sym]["bar"].get(d)
            if b is None:
                continue                              # name didn't trade this SPY day; carry forward
            exit_px = None
            # gap-aware stop: an open through the stop fills at the (worse) open, else at the stop.
            if b["open"] <= p["stop_px"]:
                exit_px = b["open"]
            elif b["low"] <= p["stop_px"]:
                exit_px = p["stop_px"]
            # trailing stop: ratchet on new highs, exit if breached intrabar
            if exit_px is None and trail > 0:
                p["hi"] = max(p["hi"], b["high"])
                tr_px = p["hi"] * (1 - trail / 100.0)
                if b["open"] <= tr_px:
                    exit_px = b["open"]
                elif b["low"] <= tr_px:
                    exit_px = tr_px
            # time exit at the close of the hold-th calendar day
            if exit_px is None and (t - p["t_entry"]) >= hold:
                exit_px = b["close"]
            if exit_px is not None:
                proceeds = p["shares"] * exit_px * (1 - cost)
                cash += proceeds
                ret = exit_px / p["entry_px"] - 1
                trades.append(ret)
                cooling[sym] = t + cooldown
                del positions[sym]

        # ---- 2. scan for new catalyst signals, fill open slots (concentrated, by gap size) ----
        slots = max_pos - len(positions)
        if slots > 0:
            cands = []
            for sym in syms:
                if sym in positions or cooling.get(sym, -1) > t:
                    continue
                dd = data.get(sym)
                if not dd or d not in dd["i"]:
                    continue
                i = dd["i"][d]
                if i < 21 or i >= len(dd["bars"]):
                    continue
                b, prev = dd["bars"][i], dd["bars"][i - 1]
                avgv = sum(dd["vols"][i - 20:i]) / 20.0
                if avgv <= 0:
                    continue
                gap = b["open"] / prev["close"] - 1
                if gap * 100 < gap_pct or b["volume"] / avgv < vol_mult:
                    continue
                cands.append((gap, sym, b["close"]))
            cands.sort(reverse=True)                  # biggest surprise first
            equity_now = cash + sum(positions[s]["shares"] * (mark(s, d) or positions[s]["entry_px"])
                                    for s in positions)
            invested = sum(positions[s]["shares"] * (mark(s, d) or positions[s]["entry_px"])
                           for s in positions)
            for gap, sym, px in cands:
                if len(positions) >= max_pos:
                    break
                budget = equity_now * pos_pct
                if invested + budget > equity_now * exposure_cap:
                    continue
                shares = int(budget // px)            # WHOLE shares (resting-stop eligible)
                spend = shares * px * (1 + cost)
                if shares < 1 or spend > cash:
                    continue
                cash -= spend
                invested += shares * px
                positions[sym] = {"shares": shares, "entry_px": px,
                                  "stop_px": px * (1 - stop / 100.0), "hi": px, "t_entry": t}

        # ---- 3. mark the book ----
        eq = cash + sum(positions[s]["shares"] * (mark(s, d) or positions[s]["entry_px"])
                        for s in positions)
        equity_curve.append(eq)
        invested_frac.append(1 - cash / eq if eq > 0 else 0)

    return {"equity": equity_curve, "invested_frac": invested_frac, "trades": trades,
            "cal": cal, "start_cash": start_cash}


def curve_stats(equity: list[float]) -> dict:
    if len(equity) < 2:
        return {"cagr": float("nan"), "max_dd": float("nan"), "sharpe": float("nan"), "total": 0}
    rets = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity)) if equity[i - 1] > 0]
    peak, max_dd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak if peak > 0 else 0)
    n_years = len(equity) / 252.0
    total = equity[-1] / equity[0] - 1
    cagr = (equity[-1] / equity[0]) ** (1 / n_years) - 1 if n_years > 0 and equity[0] > 0 else float("nan")
    mean = sum(rets) / len(rets) if rets else 0
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0
    std = math.sqrt(var)
    sharpe = mean / std * math.sqrt(252) if std > 0 else float("nan")
    return {"cagr": cagr, "max_dd": max_dd, "sharpe": sharpe, "total": total}


def spy_bh(data: dict) -> dict:
    if BENCH not in data:
        return {}
    closes = [b["close"] for b in data[BENCH]["bars"]]
    return curve_stats(closes)


def run_one(data: dict, syms: list[str], a) -> dict:
    r = sim(data, syms, a.gap, a.vol_mult, a.hold, a.stop, a.trail, a.max_pos, a.pos_pct,
            a.exposure_cap, a.cost_bps, a.cooldown, a.start_cash)
    if not r:
        return {}
    cs = curve_stats(r["equity"])
    cs["n_trades"] = len(r["trades"])
    cs["win"] = sum(1 for x in r["trades"] if x > 0) / len(r["trades"]) if r["trades"] else 0
    cs["avg_inv"] = sum(r["invested_frac"]) / len(r["invested_frac"]) if r["invested_frac"] else 0
    cs["avg_trade"] = sum(r["trades"]) / len(r["trades"]) if r["trades"] else 0
    cs["years"] = len(r["equity"]) / 252.0
    return cs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", choices=["large", "midcap", "both"], default="both")
    ap.add_argument("--gap", type=float, default=7.0, help="min overnight gap %%")
    ap.add_argument("--vol-mult", type=float, default=2.0, help="min volume vs 20d avg")
    ap.add_argument("--hold", type=int, default=15, help="max hold in trading days")
    ap.add_argument("--stop", type=float, default=8.0, help="hard stop %%")
    ap.add_argument("--trail", type=float, default=0.0, help="trailing stop %% off high-water (0=off)")
    ap.add_argument("--max-pos", type=int, default=6, help="concurrent positions (concentration)")
    ap.add_argument("--pos-pct", type=float, default=0.15, help="fraction of equity per position")
    ap.add_argument("--exposure-cap", type=float, default=0.90, help="max invested fraction")
    ap.add_argument("--cost-bps", type=float, default=15.0, help="per side")
    ap.add_argument("--cooldown", type=int, default=5, help="days before re-entering a name")
    ap.add_argument("--start-cash", type=float, default=10000.0)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()

    basket = {"large": LARGE, "midcap": MIDCAP, "both": LARGE + MIDCAP}[a.universe]
    print(f"Loading {len(basket)} names + {BENCH} (cached if present)...", file=sys.stderr)
    data = build(basket, a.refresh)
    syms = [s for s in basket if s in data]

    if a.sweep:
        print("=" * 82)
        print(f"CATALYST-BOOK SWEEP — {a.universe.upper()} | concentrated {a.max_pos}x{a.pos_pct:.0%}, "
              f"stop {a.stop}%, cost {a.cost_bps}bps")
        print("=" * 82)
        print(f"{'gap%':>5} {'hold':>5} | {'CAGR':>8} {'MaxDD':>8} {'Sharpe':>7} {'trades':>7} "
              f"{'win%':>6} {'avgInv':>7}")
        print("-" * 82)
        for g in (5, 7, 10, 15):
            for h in (5, 10, 15, 20):
                a.gap, a.hold = g, h
                cs = run_one(data, syms, a)
                if not cs:
                    continue
                print(f"{g:>5} {h:>5} | {cs['cagr']*100:>7.1f}% {cs['max_dd']*100:>7.1f}% "
                      f"{cs['sharpe']:>7.2f} {cs['n_trades']:>7} {cs['win']*100:>5.0f}% "
                      f"{cs['avg_inv']*100:>6.0f}%")
        sb = spy_bh(data)
        print("-" * 82)
        print(f"SPY buy&hold (same span): CAGR {sb.get('cagr',0)*100:.1f}%  "
              f"MaxDD {sb.get('max_dd',0)*100:.1f}%  Sharpe {sb.get('sharpe',0):.2f}")
        print("Read: a risky book should beat SPY's Sharpe AND justify its MaxDD. avgInv<100% = "
              "often partly cash (sparse signals).")
        return 0

    print("=" * 78)
    print(f"CATALYST-DRIFT BOOK BACKTEST — universe={a.universe.upper()} ({len(syms)} names)")
    print("=" * 78)
    print(f"Signal : gap>= {a.gap}% AND vol>= {a.vol_mult}x20d  |  hold<= {a.hold}d  stop -{a.stop}%"
          + (f"  trail {a.trail}%" if a.trail else ""))
    print(f"Book   : <= {a.max_pos} positions x {a.pos_pct:.0%} equity, exposure<= {a.exposure_cap:.0%}, "
          f"cost {a.cost_bps}bps/side, cooldown {a.cooldown}d, start ${a.start_cash:,.0f}")
    print("-" * 78)
    cs = run_one(data, syms, a)
    sb = spy_bh(data)
    if not cs:
        print("No result — check data.", file=sys.stderr)
        return 1
    print(f"{'':<16}{'CAGR':>9}{'MaxDD':>9}{'Sharpe':>9}{'TotRet':>10}")
    print(f"{'Catalyst book':<16}{cs['cagr']*100:>8.1f}%{cs['max_dd']*100:>8.1f}%{cs['sharpe']:>9.2f}"
          f"{cs['total']*100:>9.0f}%")
    print(f"{'SPY buy&hold':<16}{sb['cagr']*100:>8.1f}%{sb['max_dd']*100:>8.1f}%{sb['sharpe']:>9.2f}"
          f"{sb['total']*100:>9.0f}%")
    print("-" * 78)
    print(f"Trades: {cs['n_trades']}  | win {cs['win']*100:.0f}%  | avg trade {cs['avg_trade']*100:+.2f}%"
          f"  | avg invested {cs['avg_inv']*100:.0f}%  | ~{cs['years']:.1f}y")
    print("=" * 78)
    print("HONEST: survivorship/recency-biased baskets; entry at gap-day close (daily bars); the")
    print("agent's catalyst-confirmation / pump-filter is NOT modeled — this is a lower bound on clean")
    print("names and likely pessimistic on MIDCAP (filtering pumps is the agent's live job).")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
