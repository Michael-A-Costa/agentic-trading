#!/usr/bin/env python3
"""
backtest_xsection.py — cross-sectional medium-term momentum, the variant the raw-signal
backtest pointed at.

backtest_signal.py showed the engine's ABSOLUTE intraday signal ("buy whoever crossed +3% today")
is anti-predictive at 1d (short-term reversal) and only earns at 5-10d holds it never takes. This
script tests the construction that edge actually lives in: CROSS-SECTIONAL momentum — each rebalance,
rank the whole universe by trailing-return, SKIP the most recent month (to dodge the very reversal
we measured), buy the top-K strongest, hold to the next rebalance, equal weight.

This is a PORTFOLIO backtest (compounded equity curve), not per-trade expectancy. Keyless Cboe
daily OHLCV, same universe as backtest_signal.py.

=== HONEST READING ===
The universe is today's survivors -> survivorship bias inflates ALL strategies on it. So the
benchmark that matters is EQUAL-WEIGHT of the SAME universe (it carries the identical bias): the
momentum portfolio earns its keep only if it beats EW by a real margin. Absolute CAGR here is
flattered; the EW spread and its t-stat are the signal. SPY is shown as an external sanity anchor.

Usage:
  python3 scripts/backtest_xsection.py                       # 12-1 momentum, top 10, monthly
  python3 scripts/backtest_xsection.py --lookback 126 --skip 21 --topk 10 --rebalance 21
  python3 scripts/backtest_xsection.py --sweep               # grid over lookback x topk
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CACHE = REPO / "data" / "backtest" / "history"
CBOE_HIST = "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/{sym}.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BENCH = "SPY"
TRADING_DAYS = 252

# Same fixed liquid-large-cap universe as backtest_signal.py (survivorship-biased -> optimistic).
UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMD", "GOOGL", "META", "AMZN", "AVGO", "ORCL", "CRM",
    "ADBE", "INTC", "CSCO", "QCOM", "TXN", "NFLX", "DIS", "TSLA", "HD", "NKE",
    "SBUX", "MCD", "LOW", "TGT", "JPM", "BAC", "WFC", "GS", "MS", "C",
    "V", "MA", "AXP", "UNH", "JNJ", "PFE", "MRK", "ABBV", "LLY", "TMO",
    "XOM", "CVX", "CAT", "BA", "GE", "HON", "UPS", "DE", "PG", "KO",
    "PEP", "WMT", "COST", "T", "VZ", "CMCSA", "IBM", "GILD", "AMGN", "BKNG",
]


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def load_closes(sym: str, refresh: bool = False) -> dict[str, float]:
    """date -> close, from the on-disk cache backtest_signal.py already populated (or fetch)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{sym}.json"
    bars = None
    if path.exists() and not refresh:
        try:
            bars = json.loads(path.read_text())
        except Exception:
            bars = None
    if bars is None:
        try:
            d = json.loads(_http_get(CBOE_HIST.format(sym=urllib.parse.quote(sym.upper()))))
            bars = [b for b in (d.get("data") or []) if b.get("close") and b.get("open")]
            bars.sort(key=lambda b: b["date"])
            path.write_text(json.dumps(bars))
        except Exception as e:
            sys.stderr.write(f"[xsection] {sym}: fetch failed: {e}\n")
            return {}
    return {b["date"]: b["close"] for b in bars}


def equity_stats(periods: list[float], n_years: float) -> dict:
    """Compound a list of per-period simple returns into headline stats."""
    eq = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in periods:
        eq *= (1 + r)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)
    total = eq - 1
    cagr = (eq ** (1 / n_years) - 1) if n_years > 0 and eq > 0 else float("nan")
    mean = sum(periods) / len(periods) if periods else 0
    var = sum((r - mean) ** 2 for r in periods) / (len(periods) - 1) if len(periods) > 1 else 0
    std = math.sqrt(var)
    ppy = len(periods) / n_years if n_years > 0 else 0   # periods per year
    sharpe = (mean / std * math.sqrt(ppy)) if std > 0 else float("nan")
    return {"total": total, "cagr": cagr, "max_dd": max_dd, "sharpe": sharpe,
            "mean": mean, "std": std}


def run(closes: dict[str, dict[str, float]], spy_dates: list[str], lookback: int,
        skip: int, topk: int, rebalance: int, cost_bps: float) -> dict:
    """One backtest. Returns momentum/EW/SPY period-return series + the momentum-minus-EW spread."""
    start = lookback + skip
    rebal_idx = list(range(start, len(spy_dates) - 1, rebalance))
    mom_periods, ew_periods, spy_periods, diffs = [], [], [], []
    prev_set: set[str] = set()

    for i in rebal_idx:
        j = min(i + rebalance, len(spy_dates) - 1)
        if j <= i:
            break
        d_now, d_skip, d_back, d_next = (spy_dates[i], spy_dates[i - skip],
                                         spy_dates[i - lookback - skip], spy_dates[j])
        # Rank by trailing return over [d_back -> d_skip] (skips the most recent `skip` days).
        ranked = []
        avail_returns = []
        for sym, cl in closes.items():
            if sym == BENCH:                           # SPY is the external anchor, not a tradable name
                continue
            p_now, p_next = cl.get(d_now), cl.get(d_next)
            if p_now is None or p_next is None:
                continue
            fwd = p_next / p_now - 1
            avail_returns.append(fwd)                  # EW benchmark uses every tradable name
            p_skip, p_back = cl.get(d_skip), cl.get(d_back)
            if p_skip is None or p_back is None or p_back <= 0:
                continue
            ranked.append((p_skip / p_back - 1, sym, fwd))
        if not ranked or not avail_returns:
            continue
        ranked.sort(reverse=True)
        picks = ranked[:topk]
        held = {s for _, s, _ in picks}
        gross = sum(f for _, _, f in picks) / len(picks)
        # Turnover cost: names rotated in since last rebalance, charged a round trip.
        rotated = len(held - prev_set) if prev_set else len(held)
        cost = (rotated / max(len(held), 1)) * 2 * (cost_bps / 10000.0)
        prev_set = held
        mom_r = gross - cost
        ew_r = sum(avail_returns) / len(avail_returns)
        spy_r = (closes[BENCH][d_next] / closes[BENCH][d_now] - 1) if BENCH in closes else float("nan")
        mom_periods.append(mom_r)
        ew_periods.append(ew_r)
        spy_periods.append(spy_r)
        diffs.append(mom_r - ew_r)

    n_years = len(spy_dates[start:]) / TRADING_DAYS
    return {"mom": mom_periods, "ew": ew_periods, "spy": spy_periods, "diffs": diffs,
            "n_years": n_years, "n_rebal": len(mom_periods)}


def tstat(xs: list[float]) -> float:
    if len(xs) < 2:
        return float("nan")
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    sd = math.sqrt(var)
    return m / sd * math.sqrt(len(xs)) if sd > 0 else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=252, help="ranking window in trading days")
    ap.add_argument("--skip", type=int, default=21, help="recent days excluded (dodge reversal)")
    ap.add_argument("--topk", type=int, default=10)
    ap.add_argument("--rebalance", type=int, default=21, help="rebalance period in trading days")
    ap.add_argument("--cost-bps", type=float, default=10.0, help="per side, charged on turnover")
    ap.add_argument("--universe", type=str, default="",
                    help="comma-separated symbols to override the default universe (e.g. sector ETFs)")
    ap.add_argument("--drop-top", type=int, default=0,
                    help="drop the N best full-span performers (survivorship-floor sanity test)")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    universe = ([s.strip().upper() for s in args.universe.split(",") if s.strip()]
                if args.universe else list(UNIVERSE))
    print(f"Loading {len(universe)} names + {BENCH} (cached from backtest_signal.py if present)...",
          file=sys.stderr)
    closes = {BENCH: load_closes(BENCH, args.refresh)}
    for s in universe:
        closes[s] = load_closes(s, args.refresh)
    closes = {s: c for s, c in closes.items() if c}
    if BENCH not in closes:
        print("FATAL: no benchmark history.", file=sys.stderr)
        return 1
    spy_dates = sorted(closes[BENCH].keys())

    if args.drop_top > 0:
        start = args.lookback + args.skip
        start_date = spy_dates[start] if start < len(spy_dates) else spy_dates[0]
        rets = []
        for sym, cl in closes.items():
            if sym == BENCH:
                continue
            ds = sorted(d for d in cl if d >= start_date)
            if len(ds) < 2:
                continue
            rets.append((cl[ds[-1]] / cl[ds[0]] - 1, sym))   # full-span total return
        rets.sort(reverse=True)
        for _, sym in rets[:args.drop_top]:
            closes.pop(sym, None)
        print(f"[drop-top {args.drop_top}] removed best full-span performers: "
              + ", ".join(f"{s}({t*100:.0f}%)" for t, s in rets[:args.drop_top]), file=sys.stderr)

    if args.sweep:
        print("=" * 78)
        print("CROSS-SECTIONAL MOMENTUM SWEEP — CAGR (mom) | spread vs equal-weight | t-stat")
        print(f"(skip={args.skip}d, rebalance={args.rebalance}d, cost={args.cost_bps}bps/side, "
              "survivorship-biased universe)")
        print("=" * 78)
        print(f"{'lookback':>9} {'topk':>5} | {'mom CAGR':>9} {'EW CAGR':>8} "
              f"{'spread/yr':>10} {'t-stat':>7}")
        print("-" * 78)
        for lb in (63, 126, 189, 252):
            for k in (5, 10, 15):
                r = run(closes, spy_dates, lb, args.skip, k, args.rebalance, args.cost_bps)
                if not r["mom"]:
                    continue
                ms = equity_stats(r["mom"], r["n_years"])
                es = equity_stats(r["ew"], r["n_years"])
                ann_spread = (ms["cagr"] - es["cagr"])
                t = tstat(r["diffs"])
                print(f"{lb:>9} {k:>5} | {ms['cagr']*100:>8.1f}% {es['cagr']*100:>7.1f}% "
                      f"{ann_spread*100:>+9.1f}% {t:>7.2f}")
        print("-" * 78)
        print("t-stat on the per-rebalance (mom - EW) spread; |t|>~2 = unlikely to be luck.")
        print("Read the SPREAD, not the CAGR — survivorship inflates both columns equally.")
        return 0

    r = run(closes, spy_dates, args.lookback, args.skip, args.topk, args.rebalance, args.cost_bps)
    if not r["mom"]:
        print("No rebalances produced — check params/data.", file=sys.stderr)
        return 1
    ms = equity_stats(r["mom"], r["n_years"])
    es = equity_stats(r["ew"], r["n_years"])
    ss = equity_stats(r["spy"], r["n_years"])
    t = tstat(r["diffs"])
    w = 70
    print("=" * w)
    print("CROSS-SECTIONAL MOMENTUM BACKTEST  (keyless daily, portfolio/compounded)")
    print("=" * w)
    print(f"Universe     : {len([s for s in closes if s != BENCH])} large-caps "
          f"(survivorship-biased -> read the SPREAD, not CAGR)")
    print(f"Rule         : rank by trailing {args.lookback}d return, skip last {args.skip}d, "
          f"buy top {args.topk}, EW")
    print(f"Rebalance    : every {args.rebalance}d  |  cost {args.cost_bps}bps/side on turnover")
    print(f"Span         : {spy_dates[args.lookback+args.skip]} -> {spy_dates[-1]}  "
          f"(~{r['n_years']:.1f}y, {r['n_rebal']} rebalances)")
    print("-" * w)
    hdr = f"{'':<14}{'CAGR':>9}{'TotRet':>10}{'MaxDD':>9}{'Sharpe':>9}"
    print(hdr)
    for name, s in (("Momentum top-K", ms), ("Equal-weight", es), ("SPY", ss)):
        print(f"{name:<14}{s['cagr']*100:>8.1f}%{s['total']*100:>9.0f}%"
              f"{s['max_dd']*100:>8.1f}%{s['sharpe']:>9.2f}")
    print("-" * w)
    print(f"Momentum minus Equal-Weight (the real edge, both share the bias):")
    print(f"  CAGR spread       : {(ms['cagr']-es['cagr'])*100:+.1f}% / yr")
    print(f"  Per-rebalance mean : {(ms['mean']-es['mean'])*100:+.2f}%  over {r['n_rebal']} periods")
    print(f"  t-stat of spread   : {t:+.2f}   (|t|>~2 => unlikely luck)")
    print("=" * w)
    if ms["cagr"] - es["cagr"] > 0.01 and t > 1.5:
        print("VERDICT: momentum beats equal-weight with a defensible t-stat. This is the")
        print("         construction worth building the live engine around (overnight holds,")
        print("         resting stops, monthly rotation) — NOT the intraday absolute signal.")
    else:
        print("VERDICT: no robust edge over equal-weighting this universe. The apparent")
        print("         returns are survivorship + beta, not a momentum signal worth trading.")
    print("=" * w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
