#!/usr/bin/env python3
"""
kalshi_l2_collector.py — forward top-of-book collector (v0, UNAUTH, public).

WHY THIS EXISTS:
  Kalshi serves NO historical order book (only 1/60/1440-min candles + a snapshot). The
  maker / longshot-fade backtest (docs/kalshi-market-making-v1-plan.md, Phase A) therefore
  needs book data collected FORWARD. This is that collector.

DESIGN CHOICE (read docs/CLAUDE.md):
  Deliberately UNAUTH / public-data-only — no API keys, no shared secrets — to stay within
  the subtree's "public read-only market data is the ceiling until Gate 3" rule. The
  fidelity upgrade (tick-level `orderbook_delta` over WebSocket) needs an RSA-signed
  handshake = credentials = a Gate-3 secrets decision; noted in the roadmap as a later step.

HOW (efficient): one `GET /markets?status=open&series_ticker=X` call returns TOP-OF-BOOK for
  ALL of a series' open markets at once (yes/no bid+ask in dollars, top-level sizes, last,
  volume, OI). So we poll per-series, not per-market — N series = N calls/round. Full-ladder
  depth (orderbook_fp.{yes_dollars,no_dollars}) is a cheap per-market add for later.

WHAT IT LOGS per (quoted open market, tick) -> JSONL (prices in DOLLARS 0-1):
  ts, ticker, series, yes_bid, yes_ask, no_bid, no_ask, spread, mid, yes_bid_sz, yes_ask_sz,
  last, vol, oi, close.

USAGE:
  # poll top-25 liquid series' open markets every 5s for 1h:
  python3 prediction-markets/scripts/kalshi_l2_collector.py --top 25 --interval 5 \
      --duration 3600 --out prediction-markets/data/l2/run1.jsonl
  # target specific (thinner, less-contested) series:
  python3 prediction-markets/scripts/kalshi_l2_collector.py --series KXHIGHNY,KXWTI --interval 3
  # quick smoke (a few rounds):
  python3 prediction-markets/scripts/kalshi_l2_collector.py --top 4 --interval 4 --duration 9

Read-only, public data, stdlib-only. Ctrl-C to stop early (partial JSONL is kept).
"""
from __future__ import annotations
import argparse, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kalshi_pull import BASE, _get, _f, CACHE, _is_parlay


def load_target_series(top, series_arg):
    if series_arg:
        return [s.strip() for s in series_arg.split(",") if s.strip()]
    if not os.path.exists(CACHE):
        sys.exit("no discovery cache — run `kalshi_pull.py --discover` first, or pass --series")
    with open(CACHE) as f:
        return [r["series"] for r in json.load(f)[:top]]


def poll_series(series, per_series):
    """One call -> top-of-book for every OPEN market in the series. Returns quoted rows."""
    d = _get(f"{BASE}/markets?limit={per_series}&status=open&series_ticker={series}")
    if "_err" in d:
        return None
    rows = []
    for m in d.get("markets", []):
        tk = m["ticker"]
        if _is_parlay(tk):
            continue
        yb, ya = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
        nb, na = _f(m.get("no_bid_dollars")), _f(m.get("no_ask_dollars"))
        if yb is None and ya is None and nb is None and na is None:
            continue  # no live quote — skip dead/far-dated markets
        spread = (ya - yb) if (ya is not None and yb is not None) else None
        mid = ((ya + yb) / 2.0) if (ya is not None and yb is not None) else None
        rows.append({"ticker": tk, "series": series,
                     "yes_bid": yb, "yes_ask": ya, "no_bid": nb, "no_ask": na,
                     "spread": round(spread, 4) if spread is not None else None,
                     "mid": round(mid, 4) if mid is not None else None,
                     "yes_bid_sz": _f(m.get("yes_bid_size_fp")),
                     "yes_ask_sz": _f(m.get("yes_ask_size_fp")),
                     "last": _f(m.get("last_price_dollars")),
                     "vol": _f(m.get("volume_fp"), 0.0),
                     "oi": _f(m.get("open_interest_fp"), 0.0),
                     "close": m.get("close_time")})
    return rows


def run(series_list, per_series, interval, duration, out_path):
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    print(f"[collector] {len(series_list)} series, poll every {interval}s for {duration}s "
          f"-> {out_path}", file=sys.stderr)
    t_end = time.time() + duration
    rounds = snaps = errs = 0
    spreads = []
    with open(out_path, "a") as out:
        try:
            while time.time() < t_end:
                t0 = time.time()
                for s in series_list:
                    rows = poll_series(s, per_series)
                    if rows is None:
                        errs += 1
                        continue
                    for r in rows:
                        out.write(json.dumps({"ts": round(time.time(), 2), **r}) + "\n")
                        snaps += 1
                        if r["spread"] is not None:
                            spreads.append(r["spread"])
                out.flush()
                rounds += 1
                if rounds % 10 == 0:
                    ms = (sum(spreads) / len(spreads)) if spreads else float("nan")
                    print(f"[collector] round {rounds}: {snaps} snaps, mean spread "
                          f"{ms * 100:.2f}c, {errs} errs", file=sys.stderr)
                dt = time.time() - t0
                if dt < interval:
                    time.sleep(interval - dt)
        except KeyboardInterrupt:
            print("\n[collector] interrupted — partial data kept", file=sys.stderr)
    ms = (sum(spreads) / len(spreads)) if spreads else float("nan")
    print(f"\n[collector] DONE: {rounds} rounds, {snaps} snapshots, mean spread "
          f"{ms * 100:.2f}c, {errs} errs -> {out_path}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=25, help="top-N liquid series (from cache)")
    ap.add_argument("--series", default=None, help="explicit comma series list (overrides --top)")
    ap.add_argument("--per-series", type=int, default=200, help="max open markets per series call")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between snapshot rounds")
    ap.add_argument("--duration", type=float, default=3600.0, help="total seconds to collect")
    ap.add_argument("--out", default="prediction-markets/data/l2/collect.jsonl",
                    help="JSONL output path")
    a = ap.parse_args()
    series_list = load_target_series(a.top, a.series)
    run(series_list, a.per_series, a.interval, a.duration, a.out)


if __name__ == "__main__":
    main()
