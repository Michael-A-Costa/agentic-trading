#!/usr/bin/env python3
"""
backtest_signal.py — measure the RAW expectancy of the engine's entry signal (no LLM).

The live engine (tick_context.py) enters when a name is up >= SIGNAL_THRESHOLD_PCT on the
day AND >= REL_STRENGTH_PCT above SPY, then manages a STOP_LOSS_PCT / TAKE_PROFIT_PCT exit.
Everything downstream (Stage-2 DD, sizing, slippage model, resting stops) is plumbing on top
of that signal. This script answers the one question none of that plumbing answers: does the
signal itself have positive expectancy, net of costs?

It does so on KEYLESS daily OHLCV from Cboe's CDN (same source dd_probe.py uses), 2004->now,
split-adjusted.

=== WHAT THIS TESTS (and what it deliberately does NOT) ===
  TESTS:  "buy names up >= T% with >= R% rel-strength vs SPY at the signal day's close, hold up
           to H days with a STOP/TP exit (gap-aware), minus round-trip cost" — the core
           momentum-continuation premise everything rests on.
  DOES NOT model the literal ~5-min intraday entry/exit (daily bars only — min hold is overnight),
           the LLM catalyst filter (deliberately off — we want the RAW signal), or same-day churn.
  CAVEATS: the universe is a FIXED list of today's liquid large-caps -> SURVIVORSHIP BIAS, which
           biases results OPTIMISTICALLY (these names trended up to still be large today). If the
           signal loses even here, that is damning; if it wins, discount it. There is no historical
           screener, so this universe is a proxy for discover.py's eligibility filter.

The decisive output is the EDGE TEST: forward returns on SIGNAL days vs forward returns on ALL
days for the same universe. If signal-day forward returns are <= unconditional, the signal is not
predictive (short-term reversal), and no slippage/stop tuning fixes a non-edge.

Usage:
  python3 scripts/backtest_signal.py                     # defaults from .env semantics
  python3 scripts/backtest_signal.py --threshold 3 --rel 3 --stop 4 --tp 12 --hold 3
  python3 scripts/backtest_signal.py --start 2015-01-01  # restrict window
  python3 scripts/backtest_signal.py --refresh           # force re-pull history (ignore cache)
"""
from __future__ import annotations

import argparse
import json
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

# A fixed liquid-large-cap universe — proxy for discover.py's (price>=$5, mktcap>=$2B, liquid)
# eligibility filter. SURVIVORSHIP-BIASED (today's survivors) => optimistic; stated in the report.
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


def load_bars(sym: str, refresh: bool = False) -> list[dict]:
    """Daily OHLCV bars [{date, open, high, low, close, volume}, ...] ascending. Cached on disk."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{sym}.json"
    if path.exists() and not refresh:
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    try:
        d = json.loads(_http_get(CBOE_HIST.format(sym=urllib.parse.quote(sym.upper()))))
        bars = d.get("data") or []
        bars = [b for b in bars if b.get("close") and b.get("open")]
        bars.sort(key=lambda b: b["date"])
        path.write_text(json.dumps(bars))
        return bars
    except Exception as e:
        sys.stderr.write(f"[backtest] {sym}: history fetch failed: {e}\n")
        return []


def simulate(entry: float, fwd: list[dict], stop_pct: float, tp_pct: float,
             max_hold: int) -> tuple[float, str, int]:
    """Forward-walk a trade from `entry` over `fwd` bars. Gap-aware: an open beyond a level fills
    at the open (worse than the level), capturing gap risk the synthetic stop cannot cover. Stop is
    checked before TP within a bar (conservative). Returns (gross_return, exit_reason, days_held)."""
    stop = entry * (1 - stop_pct / 100.0)
    tp = entry * (1 + tp_pct / 100.0)
    horizon = fwd[:max_hold]
    for i, b in enumerate(horizon):
        o, h, l, c = b["open"], b["high"], b["low"], b["close"]
        if o <= stop:                      # gapped down through the stop
            return o / entry - 1, "stop_gap", i + 1
        if o >= tp:                        # gapped up through the target
            return o / entry - 1, "tp_gap", i + 1
        if l <= stop:                      # intrabar stop
            return stop / entry - 1, "stop", i + 1
        if h >= tp:                        # intrabar target
            return tp / entry - 1, "tp", i + 1
        if i == len(horizon) - 1:          # ran out of hold window -> exit at close
            return c / entry - 1, "max_hold", i + 1
    return 0.0, "no_data", 0


def pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=3.0, help="min daily %% move vs prev close")
    ap.add_argument("--rel", type=float, default=3.0, help="min %% above SPY's same-day move")
    ap.add_argument("--stop", type=float, default=4.0)
    ap.add_argument("--tp", type=float, default=12.0)
    ap.add_argument("--hold", type=int, default=3, help="max hold in trading days")
    ap.add_argument("--cost-bps", type=float, default=10.0, help="cost per side in bps (round trip = 2x)")
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    print(f"Loading daily history for {len(UNIVERSE)} names + {BENCH} from Cboe (keyless)...",
          file=sys.stderr)
    bench_bars = load_bars(BENCH, args.refresh)
    if not bench_bars:
        print("FATAL: could not load benchmark history.", file=sys.stderr)
        return 1
    spy_ret = {}  # date -> SPY daily return vs prev close
    for i in range(1, len(bench_bars)):
        p, c = bench_bars[i - 1]["close"], bench_bars[i]["close"]
        spy_ret[bench_bars[i]["date"]] = c / p - 1

    trades: list[dict] = []
    # Edge test accumulators: forward H-day close-to-close return, signal-days vs all-days.
    sig_fwd: list[float] = []
    all_fwd: list[float] = []

    for sym in UNIVERSE:
        bars = load_bars(sym, args.refresh)
        if len(bars) < args.hold + 2:
            continue
        for i in range(1, len(bars) - 1):
            b, prev = bars[i], bars[i - 1]
            date = b["date"]
            if date < args.start or date > args.end:
                continue
            day_ret = b["close"] / prev["close"] - 1
            fwd = bars[i + 1: i + 1 + args.hold]
            if not fwd:
                continue
            # Unconditional baseline: every day's forward H-day close-to-close return.
            all_fwd.append(fwd[-1]["close"] / b["close"] - 1)
            # Signal: up >= threshold AND >= rel above SPY that day.
            s = spy_ret.get(date)
            rel = day_ret - s if s is not None else day_ret
            if day_ret * 100 < args.threshold or rel * 100 < args.rel:
                continue
            sig_fwd.append(fwd[-1]["close"] / b["close"] - 1)
            gross, reason, held = simulate(b["close"], fwd, args.stop, args.tp, args.hold)
            net = gross - 2 * args.cost_bps / 10000.0
            trades.append({"sym": sym, "date": date, "gross": gross, "net": net,
                           "reason": reason, "held": held})

    if not trades:
        print("No trades triggered for these parameters.", file=sys.stderr)
        return 1

    n = len(trades)
    nets = [t["net"] for t in trades]
    grosses = [t["gross"] for t in trades]
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    avg_net = sum(nets) / n
    avg_gross = sum(grosses) / n
    win_rate = len(wins) / n
    avg_win = sum(t["net"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["net"] for t in losses) / len(losses) if losses else 0
    gross_profit = sum(t["net"] for t in wins)
    gross_loss = -sum(t["net"] for t in losses)
    pf = gross_profit / gross_loss if gross_loss else float("inf")
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    sig_mean = sum(sig_fwd) / len(sig_fwd) if sig_fwd else 0
    all_mean = sum(all_fwd) / len(all_fwd) if all_fwd else 0

    w = 64
    print("=" * w)
    print("RAW SIGNAL BACKTEST  (no LLM, daily bars, Cboe keyless OHLCV)")
    print("=" * w)
    print(f"Universe        : {len(UNIVERSE)} liquid large-caps (survivorship-biased -> optimistic)")
    print(f"Window          : {args.start} -> {args.end}")
    print(f"Signal          : day move >= {args.threshold}%  AND  >= {args.rel}% vs SPY")
    print(f"Exits           : stop -{args.stop}% / tp +{args.tp}% / max-hold {args.hold}d, gap-aware")
    print(f"Cost            : {args.cost_bps} bps/side ({2*args.cost_bps:.0f} bps round trip)")
    print("-" * w)
    print(f"Trades          : {n}")
    print(f"Win rate        : {win_rate*100:.1f}%   ({len(wins)}W / {len(losses)}L)")
    print(f"Avg trade (net) : {pct(avg_net)}      <-- EXPECTANCY per trade")
    print(f"Avg trade(gross): {pct(avg_gross)}   (before the {2*args.cost_bps:.0f}bps round trip)")
    print(f"Avg win / loss  : {pct(avg_win)} / {pct(avg_loss)}")
    print(f"Profit factor   : {pf:.2f}   (>1 = profitable, net)")
    print(f"Total net (sum %): {pct(sum(nets))}  across all trades (not compounded)")
    print(f"Exit breakdown  : " + ", ".join(f"{k}={v}" for k, v in sorted(reasons.items())))
    print("-" * w)
    print("EDGE TEST  (is the signal predictive at all?)")
    print(f"  Fwd {args.hold}d return, SIGNAL days : {pct(sig_mean)}   (n={len(sig_fwd)})")
    print(f"  Fwd {args.hold}d return, ALL days    : {pct(all_mean)}   (n={len(all_fwd)})")
    edge = sig_mean - all_mean
    verdict = "PREDICTIVE (momentum)" if edge > 0 else "ANTI-PREDICTIVE (reversal)"
    print(f"  Conditional edge          : {pct(edge)}   -> {verdict}")
    print("=" * w)
    if avg_net <= 0:
        print("VERDICT: negative expectancy net of costs. Tuning slippage/stops will not")
        print("         create an edge that the raw signal does not have.")
    elif edge <= 0:
        print("VERDICT: trades may look ok but the signal underperforms buying ANY day —")
        print("         the exit geometry is doing the work, not the entry. Fragile.")
    else:
        print("VERDICT: positive expectancy AND a real conditional edge. Worth refining.")
    print("=" * w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
