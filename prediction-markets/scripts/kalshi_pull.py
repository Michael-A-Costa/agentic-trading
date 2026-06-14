#!/usr/bin/env python3
"""
kalshi_pull.py — coverage/feasibility probe for the "near-certain favorite" idea.

GOAL (not a live strategy — a data-pull to size the backtest):
  Buy the favorite side (YES or NO) in markets that, H hours before close, trade
  at >= THRESHOLD (default 0.90), hold to settlement. This script pulls *settled*
  Kalshi markets from the public REST API, reconstructs the price H hours before
  close from candlesticks (using the **ask** = real buy cost, not mid), and reports:

    - coverage: how many settled markets / how many qualify at the threshold
    - calibration: did >=90% favorites actually resolve in their favor >=90%?
    - economics: mean realized return NET of Kalshi's fee model, win rate,
      and the negative-skew tail (worst loss, % that resolved against the favorite)

  This answers "is there a backtestable, fee-survivable edge here, and how big is
  the sample?" before any capital, wallet, or live wiring. Public data only.

USAGE:
  python3 scripts/kalshi_pull.py                      # default series, 48h horizon
  python3 scripts/kalshi_pull.py --horizon-hours 24 --threshold 0.92
  python3 scripts/kalshi_pull.py --series KXHIGHNY,KXBTCD --max-markets 80
  python3 scripts/kalshi_pull.py --out data/kalshi_favorites.jsonl

Stdlib-only (urllib). Read-only against api.elections.kalshi.com.
"""
from __future__ import annotations
import argparse, json, math, sys, time, urllib.request, urllib.error, datetime as dt
from collections import Counter

BASE = "https://api.elections.kalshi.com/trade-api/v2"
UA = {"User-Agent": "agentic-trading-research/kalshi_pull"}

# Curated liquid, short-dated series across categories. Weather + crypto produce
# the most near-certain favorites close to settlement (the population we care about).
DEFAULT_SERIES = [
    "KXHIGHNY", "KXHIGHLAX", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHAUS",  # daily city high temps
    "KXBTCD", "KXETHD",                                              # daily crypto ranges
    "KXFEDDECISION",                                                 # macro
]

# Kalshi published general trading fee: fee = ceil(0.07 * C * P * (1-P)) in cents,
# rounded up per order. Per-contract (large order) limit -> 0.07 * P * (1-P).
# This is SMALLEST at the extremes, which is favorable for a 0.90+ strategy.
FEE_COEF = 0.07


def _throttle_get(url: str, tries: int = 5, pause: float = 0.20) -> dict:
    """GET with polite throttle + 429 backoff. Returns dict, or {'_err': code}."""
    time.sleep(pause)
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(1.5 * (i + 1))
                continue
            return {"_err": e.code}
        except Exception as e:  # noqa: BLE001
            return {"_err": str(e)}
    return {"_err": "429-giveup"}


def iter_settled_markets(series: str, max_markets: int):
    """Yield settled markets for a series, paginating until max_markets."""
    cursor, got = None, 0
    while got < max_markets:
        url = f"{BASE}/markets?limit=200&status=settled&series_ticker={series}"
        if cursor:
            url += f"&cursor={cursor}"
        d = _throttle_get(url)
        if "_err" in d:
            print(f"  ! {series}: markets err {d['_err']}", file=sys.stderr)
            return
        for m in d.get("markets", []):
            yield m
            got += 1
            if got >= max_markets:
                return
        cursor = d.get("cursor")
        if not cursor:
            return


def _ts(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def snapshot_at_horizon(series: str, ticker: str, close_ts: int, horizon_h: float):
    """
    Return (yes_ask, yes_bid) at ~horizon_h hours before close, from candlesticks.
    Picks the last candle at or before the target time. None if no usable history.
    """
    target = close_ts - int(horizon_h * 3600)
    start = target - 6 * 3600  # small window around the target
    end = close_ts
    url = (f"{BASE}/series/{series}/markets/{ticker}/candlesticks"
           f"?start_ts={start}&end_ts={end}&period_interval=60")
    d = _throttle_get(url)
    if "_err" in d:
        return None
    candles = d.get("candlesticks", [])
    pick = None
    for c in candles:
        if c.get("end_period_ts", 0) <= target:
            pick = c
        else:
            break
    if pick is None:  # nothing before target; take earliest available after
        pick = candles[0] if candles else None
    if pick is None:
        return None
    yes_ask = _f((pick.get("yes_ask") or {}).get("close_dollars"))
    yes_bid = _f((pick.get("yes_bid") or {}).get("close_dollars"))
    vol = _f(pick.get("volume_fp"), 0.0)
    return yes_ask, yes_bid, vol


def fee_per_contract(price: float) -> float:
    """Kalshi general fee, per-contract continuous approximation."""
    return FEE_COEF * price * (1.0 - price)


def analyze(series_list, horizon_h, threshold, max_markets, min_vol, out_path):
    rows = []
    n_markets = n_with_candles = n_skipped_mve = 0

    for series in series_list:
        print(f"[pull] {series} ...", file=sys.stderr)
        for m in iter_settled_markets(series, max_markets):
            tick = m["ticker"]
            if "MVE" in tick or "MULTI" in tick:  # skip auto-generated parlays
                n_skipped_mve += 1
                continue
            result = m.get("result")
            if result not in ("yes", "no"):
                continue
            n_markets += 1
            snap = snapshot_at_horizon(series, tick, _ts(m["close_time"]), horizon_h)
            if not snap:
                continue
            yes_ask, yes_bid, vol = snap
            if yes_ask is None or yes_bid is None:
                continue
            n_with_candles += 1

            # Favorite = the side you'd pay >= threshold to buy.
            # Buy YES at yes_ask; buy NO at no_ask = 1 - yes_bid.
            no_ask = 1.0 - yes_bid
            side = price = None
            if yes_ask >= threshold:
                side, price, won = "yes", yes_ask, (result == "yes")
            elif no_ask >= threshold:
                side, price, won = "no", no_ask, (result == "no")
            else:
                continue
            if price >= 1.0 or vol < min_vol:
                continue

            fee = fee_per_contract(price)
            payoff = 1.0 if won else 0.0
            net_pnl = payoff - price - fee          # $ per contract
            net_ret = net_pnl / price               # return on capital staked
            rows.append({
                "series": series, "ticker": tick, "result": result,
                "side": side, "entry_price": round(price, 4),
                "fee": round(fee, 5), "won": won,
                "net_pnl": round(net_pnl, 5), "net_ret": round(net_ret, 5),
                "horizon_h": horizon_h, "vol_at_snap": vol,
            })

    _report(rows, n_markets, n_with_candles, n_skipped_mve,
            horizon_h, threshold, min_vol)
    if out_path and rows:
        with open(out_path, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        print(f"\n[out] wrote {len(rows)} qualifying rows -> {out_path}")


def _report(rows, n_markets, n_with_candles, n_skipped, horizon_h, threshold, min_vol):
    print("\n" + "=" * 68)
    print(f"KALSHI 'near-certain favorite' coverage probe")
    print(f"  horizon={horizon_h}h before close | buy favorite at ask >= {threshold}"
          f" | min snap vol={min_vol}")
    print("=" * 68)
    print(f"settled single markets scanned : {n_markets}")
    print(f"  with usable candle history   : {n_with_candles}")
    print(f"  MVE/parlay markets skipped   : {n_skipped}")
    print(f"QUALIFYING bets (>= threshold) : {len(rows)}")
    if not rows:
        print("\nNo qualifying markets — widen --series, --max-markets, or lower --threshold.")
        return

    wins = sum(r["won"] for r in rows)
    n = len(rows)
    win_rate = wins / n
    mean_price = sum(r["entry_price"] for r in rows) / n
    mean_net = sum(r["net_pnl"] for r in rows) / n          # $/contract
    mean_ret = sum(r["net_ret"] for r in rows) / n          # on capital
    worst = min(r["net_ret"] for r in rows)
    losses = [r for r in rows if not r["won"]]

    print(f"\n--- CALIBRATION (the steamroller check) ---")
    print(f"  mean entry price (implied p) : {mean_price:.3f}")
    print(f"  realized favorite win-rate   : {win_rate:.3f}  ({wins}/{n})")
    edge = win_rate - mean_price
    print(f"  calibration edge (real-impl) : {edge:+.3f}  "
          f"({'favorites UNDERpriced (good)' if edge>0 else 'favorites OVERpriced/efficient'})")

    print(f"\n--- ECONOMICS (net of modeled fee) ---")
    print(f"  mean net P&L   : {mean_net:+.4f} $/contract")
    print(f"  mean net return: {mean_ret:+.4%} on capital staked")
    print(f"  bets that LOST : {len(losses)}/{n} ({len(losses)/n:.1%})  "
          f"avg loss {sum(r['net_ret'] for r in losses)/max(len(losses),1):.1%}")
    print(f"  worst single   : {worst:.1%}")
    # crude t-stat on per-bet net return (independence is optimistic — flagged below)
    if n > 1:
        m = mean_ret
        sd = (sum((r["net_ret"] - m) ** 2 for r in rows) / (n - 1)) ** 0.5
        t = m / (sd / math.sqrt(n)) if sd > 0 else float("nan")
        print(f"  naive t-stat   : {t:.2f}  (assumes independent bets — OPTIMISTIC; "
              f"resolutions cluster)")

    print(f"\n--- by price bucket ---")
    buckets = {}
    for r in rows:
        b = min(int(r["entry_price"] * 20) / 20, 0.95)  # 5c buckets
        buckets.setdefault(b, []).append(r)
    for b in sorted(buckets):
        g = buckets[b]
        wr = sum(x["won"] for x in g) / len(g)
        nr = sum(x["net_ret"] for x in g) / len(g)
        print(f"  [{b:.2f}-{b+0.05:.2f}) n={len(g):4d}  win={wr:.3f}  net_ret={nr:+.2%}")

    print(f"\n--- by series ---")
    for s, c in Counter(r["series"] for r in rows).most_common():
        print(f"  {s:16s} n={c}")

    print("\nCAVEATS: candle ask captures spread but not depth/partial fills; fee is the "
          "\npublished general formula (per-contract limit); t-stat ignores correlated "
          "\nresolutions; sample here is weather/crypto-heavy. This is a coverage probe, "
          "\nnot a validated edge — feed rows into the H2 backtest gate before any capital.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", default=",".join(DEFAULT_SERIES),
                    help="comma-separated Kalshi series tickers")
    ap.add_argument("--horizon-hours", type=float, default=48.0,
                    help="hours before close to snapshot the price (default 48)")
    ap.add_argument("--threshold", type=float, default=0.90,
                    help="min favorite ask to qualify (default 0.90)")
    ap.add_argument("--max-markets", type=int, default=120,
                    help="max settled markets per series (default 120)")
    ap.add_argument("--min-vol", type=float, default=0.0,
                    help="min candle volume at snapshot to count (liquidity filter)")
    ap.add_argument("--out", default=None, help="optional JSONL path for qualifying rows")
    a = ap.parse_args()
    analyze([s.strip() for s in a.series.split(",") if s.strip()],
            a.horizon_hours, a.threshold, a.max_markets, a.min_vol, a.out)


if __name__ == "__main__":
    main()
