#!/usr/bin/env python3
"""
catalyst_filter_report.py — does the agentic pump-filter actually lift the drift? (forward, leakage-free)

Joins each logged gap event (data/catalyst_events.jsonl) to its realized forward drift from keyless
daily history, then compares the agent's calls against the unconditional "gap alone" baseline:

  ALL   = every gap event we evaluated            (= "gap alone", the no-filter baseline)
  REAL  = agent COMMIT (a real, tradeable catalyst)
  PUMP  = agent REJECT with no catalyst            (the pumps/squeezes the filter is meant to drop)
  OTHER = agent REJECT for a risk reason           (real catalyst, passed — e.g. earnings blackout)

The question: does conditioning on REAL lift the median forward drift above ALL (and above PUMP)?
If yes, the agent's catalyst-confirmation earns its keep — the one lever the historical backtest
can't measure honestly (see catalyst_log.py on why this must be forward, not a backtest).

Drift is measured from the event's ref_price (the entry basis at evaluation) to the close `--horizon`
TRADING days later — a raw signal measure, no stop applied (the stop is a separate execution choice).

Usage:
  python3 scripts/catalyst_filter_report.py              # 15 trading-day horizon
  python3 scripts/catalyst_filter_report.py --horizon 10
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import market_conditions as mc  # daily_bars_cached (keyless Cboe) + ET

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "data" / "catalyst_events.jsonl"


def forward_drift(bars: list[dict], eval_date: str, ref_price: float, horizon: int) -> float | None:
    """Return-from-ref_price to the close `horizon` trading days after the eval bar, or None if not yet
    mature / locatable. Pure (unit-testable): bars = [{date, close, ...}] oldest->newest."""
    if not bars or not ref_price or ref_price <= 0:
        return None
    # locate the eval bar: exact date, else the last completed bar on/before eval_date
    idx = None
    for i, b in enumerate(bars):
        if b.get("date") == eval_date:
            idx = i
            break
    if idx is None:
        prior = [i for i, b in enumerate(bars) if b.get("date") and b["date"] <= eval_date]
        if not prior:
            return None
        idx = prior[-1]
    fwd = idx + horizon
    if fwd >= len(bars):
        return None                      # not enough forward bars yet — event is still pending
    fwd_close = bars[fwd].get("close")
    if not fwd_close:
        return None
    return fwd_close / ref_price - 1.0


def _median(xs: list[float]) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _stats(drifts: list[float]) -> dict:
    n = len(drifts)
    if n == 0:
        return {"n": 0, "median": float("nan"), "mean": float("nan"), "win": float("nan")}
    return {"n": n, "median": _median(drifts), "mean": sum(drifts) / n,
            "win": sum(1 for x in drifts if x > 0) / n}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=15, help="forward trading days (drift window)")
    ap.add_argument("--refresh", action="store_true", help="re-fetch daily history (else daily cache)")
    args = ap.parse_args()

    if not LEDGER.exists():
        print(f"No events yet ({LEDGER.relative_to(REPO)}). The paper engine logs one per evaluated "
              "gap candidate; let it run through some market-hours ticks first.")
        return 0
    events = [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()]
    today = datetime.now(mc.ET).strftime("%Y-%m-%d")

    # Resolve each event's forward drift from its symbol's daily history (cached per day).
    bars_cache: dict[str, list] = {}
    buckets: dict[str, list] = {"ALL": [], "REAL": [], "PUMP": [], "OTHER": []}
    resolved = pending = 0
    for e in events:
        sym, ref = e.get("symbol"), e.get("ref_price")
        if not sym or not ref:
            continue
        if sym not in bars_cache:
            bars_cache[sym] = mc.daily_bars_cached(sym, today) if not args.refresh else mc._fetch_daily_ohlcv(sym)
        d = forward_drift(bars_cache[sym], e.get("eval_date"), ref, args.horizon)
        if d is None:
            pending += 1
            continue
        resolved += 1
        buckets["ALL"].append(d)
        if e.get("is_real"):
            buckets["REAL"].append(d)
        elif e.get("is_pump"):
            buckets["PUMP"].append(d)
        else:
            buckets["OTHER"].append(d)

    w = 64
    print("=" * w)
    print(f"CATALYST FILTER LIFT — forward {args.horizon}-trading-day drift, leakage-free")
    print("=" * w)
    print(f"Events logged: {len(events)}  |  resolved: {resolved}  |  pending (too recent): {pending}")
    print("-" * w)
    print(f"{'bucket':<22}{'n':>5}{'median':>10}{'mean':>10}{'win%':>8}")
    labels = {"ALL": "ALL (gap alone)", "REAL": "REAL (agent commit)",
              "PUMP": "PUMP (reject/none)", "OTHER": "OTHER (reject/risk)"}
    st = {k: _stats(v) for k, v in buckets.items()}
    for k in ("ALL", "REAL", "PUMP", "OTHER"):
        s = st[k]
        if s["n"] == 0:
            print(f"{labels[k]:<22}{0:>5}{'--':>10}{'--':>10}{'--':>8}")
        else:
            print(f"{labels[k]:<22}{s['n']:>5}{s['median']*100:>9.2f}%{s['mean']*100:>9.2f}%"
                  f"{s['win']*100:>7.0f}%")
    print("-" * w)
    real, allb, pump = st["REAL"], st["ALL"], st["PUMP"]
    if real["n"] >= 3 and allb["n"] >= 3:
        lift_vs_all = (real["median"] - allb["median"]) * 100
        print(f"LIFT  REAL median - ALL median : {lift_vs_all:+.2f}%   (filter vs no-filter)")
        if pump["n"] >= 3:
            print(f"      REAL median - PUMP median: {(real['median']-pump['median'])*100:+.2f}%   "
                  "(does it separate real from pump?)")
        verdict = ("the filter ADDS value (REAL drifts above the unconditional gap)"
                   if lift_vs_all > 0 else "NO lift yet — REAL is not beating the average gap event")
        print(f"VERDICT: {verdict}")
    else:
        print(f"Not enough resolved events yet (need >=3 REAL & >=3 ALL; have "
              f"{real['n']} REAL, {allb['n']} ALL). Let the engine accumulate.")
    print("=" * w)
    print("HONEST: forward measure (no hindsight) but small-N early; drift from the eval-time ref price "
          "with NO stop applied; only the top-MAX_DD_CANDIDATES gappers/tick are agent-scored; the "
          "universe is whatever gapped (survivorship). Read it as it grows, not on the first handful.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
