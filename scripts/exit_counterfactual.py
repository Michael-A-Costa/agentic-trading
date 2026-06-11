#!/usr/bin/env python3
"""
exit_counterfactual.py — let-run replay on ACTUAL fills: the validation arm that replaced the
paper gate when DISCO_EXITS_LIVE flipped to 1 (2026-06-11, exit-strategy-findings §5).

For every closed round-trip in the trade history, replay the LET-RUN exit schedule (the
pre-flip live policy: stop/softcut/breakeven + trail + far TP from .env) on the same symbol,
entry date and entry FILL price using keyless daily history, and compare with what the active
policy actually realized. Because both arms share the identical entries, this measures the
exit policy alone — no venue/cadence/sizing confounds (the weakness of a paper-vs-live
comparison).

Honesty notes:
- ACTUAL returns come from real fills as recorded (real friction already in the prices); the
  counterfactual uses theoretical daily-bar prices minus --cost-bps per leg.
- The replay is daily-bar: entry-day action after the fill is not replayed; exits fire from
  day+1 on highs/lows (same engine as the backtests, backtest_quickwin.simulate).
- A trade whose counterfactual is still running (younger than --hold trading days and not yet
  stopped/TP'd in replay) is marked PARTIAL and excluded from the aggregate.

Usage:
  python3 scripts/exit_counterfactual.py                      # live trades, all books
  python3 scripts/exit_counterfactual.py --book disco --since 2026-06-11
  python3 scripts/exit_counterfactual.py --mode paper --hold 21
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_gap_drift as gd
import backtest_quickwin as qw
import trade_ledger as tl

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "data" / "trades.jsonl"


def envf(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "") or default)
    except ValueError:
        return default


def letrun_policy(cost_bps: float) -> dict:
    """The pre-flip live exit schedule, read from .env so the counterfactual tracks the
    actual let-run config (stop12/sc8/be12 + trail15@20 + tp40 at today's values)."""
    pol = {"stop": envf("STOP_LOSS_PCT", 12.0), "softcut": envf("SOFT_CUT_PCT", 8.0),
           "be": envf("TRAIL_BREAKEVEN_AT_PCT", 12.0), "tp": envf("TAKE_PROFIT_PCT", 40.0),
           "cost_bps": cost_bps}
    trail, act = envf("TRAIL_STOP_PCT", 15.0), envf("TRAIL_ACTIVATE_PCT", 20.0)
    if trail > 0:
        pol["trail"], pol["activate"] = trail, act
    return pol


def round_trips_with_meta(rows: list[dict]) -> list[dict]:
    """FIFO entry/exit pairing like trade_ledger.build_round_trips, but carrying the buy row's
    entry date and book (needed to align the replay and split the aggregate)."""
    open_lots: dict[str, deque] = defaultdict(deque)  # sym -> deque of [qty, price, date, book]
    trips: list[dict] = []
    for r in rows:
        sym = str(r.get("symbol", "")).upper()
        side = str(r.get("side", "")).lower()
        qty = float(r.get("qty") or 0.0)
        price = r.get("price")
        date = (r.get("ts_et") or r.get("ts_utc") or "")[:10]
        if qty <= 0 or price is None or not date:
            continue
        if side == "buy":
            open_lots[sym].append([qty, float(price), date, r.get("book") or "untagged"])
            continue
        remaining = qty
        lots = open_lots[sym]
        while remaining > 1e-9 and lots:
            lot = lots[0]
            take = min(remaining, lot[0])
            trips.append({"symbol": sym, "qty": take, "entry_price": lot[1],
                          "entry_date": lot[2], "book": lot[3],
                          "exit_price": float(price), "exit_date": date,
                          "exit_type": r.get("exit_type", "other")})
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-9:
                lots.popleft()
    return trips


def bars_for(sym: str, exit_date: str, cache: dict, refresh: bool) -> list[dict]:
    """Cleaned daily bars, auto-refetching when the cached history doesn't reach past the
    exit (the counterfactual needs post-exit bars to keep running the let-run schedule)."""
    if sym not in cache:
        bars = gd.clean_bars(gd.load_bars(sym, refresh))
        if (not bars or bars[-1]["date"] <= exit_date) and not refresh:
            bars = gd.clean_bars(gd.load_bars(sym, True))
        cache[sym] = bars
    return cache[sym]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="live", help="live | paper | all (default live)")
    ap.add_argument("--book", default=None, help="pead | disco | untagged (default all)")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD floor on trade timestamps")
    ap.add_argument("--hold", type=int, default=int(envf("MAX_HOLD_DAYS", 21)),
                    help="counterfactual horizon in trading days (default MAX_HOLD_DAYS)")
    ap.add_argument("--cost-bps", type=float, default=15.0)
    ap.add_argument("--refresh", action="store_true", help="refetch all price history")
    args = ap.parse_args()

    rows = tl.load_rows(LEDGER, args.since, None, args.mode, args.book)
    trips = round_trips_with_meta(rows)
    if not trips:
        print("no closed round-trips match the filters")
        return 0

    pol = letrun_policy(args.cost_bps)
    pol_desc = (f"stop{pol['stop']:.0f}/sc{pol['softcut']:.0f}/be{pol['be']:.0f}"
                f"/tp{pol['tp']:.0f}" + (f"/tr{pol['trail']:.0f}@{pol['activate']:.0f}"
                                         if "trail" in pol else ""))
    print(f"LET-RUN counterfactual ({pol_desc}, hold {args.hold}d, {args.cost_bps:.0f}bps/leg) "
          f"vs ACTUAL exits — {len(trips)} closed round-trip(s)\n")
    hdr = (f"{'exit date':<11}{'sym':<6}{'book':<9}{'entry':>8}{'exit':>8}{'actual%':>8} "
           f"{'exit_type':<12}{'cf%':>7}{'cf_day':>7}  {'delta':>7}")
    print(hdr)
    print("-" * len(hdr))

    cache: dict = {}
    complete, partial, unpriced = [], [], []
    for t in trips:
        sym = t["symbol"]
        actual = t["exit_price"] / t["entry_price"] - 1.0
        try:
            bars = bars_for(sym, t["exit_date"], cache, args.refresh)
        except Exception:
            bars = []
        i = next((k for k, b in enumerate(bars) if b["date"] >= t["entry_date"]), None)
        if i is None or not bars or bars[i]["date"] != t["entry_date"]:
            unpriced.append(t)
            print(f"{t['exit_date']:<11}{sym:<6}{t['book']:<9}{t['entry_price']:>8.2f}"
                  f"{t['exit_price']:>8.2f}{actual * 100:>+7.2f}% {t['exit_type']:<12}"
                  f"{'no bars':>7}")
            continue
        horizon = min(args.hold, len(bars) - 1 - i)
        if horizon < 1:
            unpriced.append(t)
            continue
        cf, cf_day = qw.simulate(bars, i, t["entry_price"], horizon, pol)[:2]
        is_partial = horizon < args.hold and cf_day == horizon  # still running in replay
        rec = dict(t, actual=actual, cf=cf, cf_day=cf_day, delta=actual - cf)
        (partial if is_partial else complete).append(rec)
        print(f"{t['exit_date']:<11}{sym:<6}{t['book']:<9}{t['entry_price']:>8.2f}"
              f"{t['exit_price']:>8.2f}{actual * 100:>+7.2f}% {t['exit_type']:<12}"
              f"{cf * 100:>+6.2f}%{cf_day:>6}d  {(actual - cf) * 100:>+6.2f}%"
              + ("  PARTIAL" if is_partial else ""))

    def agg(label: str, recs: list[dict]) -> None:
        if not recs:
            return
        a = [r["actual"] for r in recs]
        c = [r["cf"] for r in recs]
        d = [r["delta"] for r in recs]
        n = len(recs)
        print(f"\n{label} (n={n}):")
        print(f"  actual : mean {sum(a) / n * 100:+.2f}%  median {gd.median(a) * 100:+.2f}%  "
              f"win {sum(1 for x in a if x > 0) / n * 100:.0f}%")
        print(f"  let-run: mean {sum(c) / n * 100:+.2f}%  median {gd.median(c) * 100:+.2f}%  "
              f"win {sum(1 for x in c if x > 0) / n * 100:.0f}%")
        print(f"  delta  : mean {sum(d) / n * 100:+.2f}%/trade  "
              f"(positive = active policy beat let-run on these entries)")

    agg("ALL COMPLETE", complete)
    for book in sorted({r["book"] for r in complete}):
        agg(f"book={book}", [r for r in complete if r["book"] == book])
    if partial:
        print(f"\n{len(partial)} PARTIAL row(s) excluded from aggregates "
              f"(counterfactual still running — re-run after more sessions)")
    if unpriced:
        print(f"{len(unpriced)} round-trip(s) had no usable daily history (see 'no bars' rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
