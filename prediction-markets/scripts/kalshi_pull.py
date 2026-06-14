#!/usr/bin/env python3
"""
kalshi_pull.py — coverage/feasibility probe for the "near-certain favorite" idea,
scaled across the liquid Kalshi universe.

GOAL (not a live strategy — a data-pull to size the backtest):
  Buy the favorite side (YES or NO) in markets that, H hours before close, trade
  at >= THRESHOLD (default 0.90), hold to settlement. Pull *settled* Kalshi markets
  from the public REST API, reconstruct the price H hours before close from
  candlesticks (using the **ask** = real buy cost, not mid), and report calibration
  + economics net of Kalshi's fee model.

UNIVERSE / DISCOVERY:
  Kalshi has ~10.8k series but the tradable feed is ~99.99% auto-generated MVE
  parlays. The liquid "ending soon" universe is the recurring short-dated series
  (daily/weekly/hourly/15min ~= 510 series). --discover ranks those by aggregate
  open interest and caches the ranked list; the sweep then runs the top --top of them.

USAGE:
  # 1) build/refresh the liquid-series cache (ranks ~510 recurring series by OI):
  python3 prediction-markets/scripts/kalshi_pull.py --discover
  # 2) sweep the top liquid series at one or more horizons:
  python3 prediction-markets/scripts/kalshi_pull.py --top 50 --horizon-hours 6,24,48 \
      --threshold 0.90 --min-vol 50 --out prediction-markets/data/favorites.jsonl
  # override the universe explicitly:
  python3 prediction-markets/scripts/kalshi_pull.py --series KXHIGHNY,KXBTCD

Stdlib-only (urllib). Read-only against api.elections.kalshi.com. Public data.
"""
from __future__ import annotations
import argparse, json, math, os, sys, time, urllib.request, urllib.error, datetime as dt

BASE = "https://api.elections.kalshi.com/trade-api/v2"
UA = {"User-Agent": "agentic-trading-research/kalshi_pull"}
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "data"))
CACHE = os.path.join(DATA, "kalshi_series_liquid.json")

# Recurring short-dated frequencies = the liquid "ending soon" universe.
SHORT_FREQS = {"daily", "weekly", "hourly", "fifteen_min"}

# Kalshi published general trading fee: fee = ceil(0.07 * C * P * (1-P)) cents per order;
# per-contract (large order) limit -> 0.07 * P * (1-P). Smallest at the extremes ->
# favorable for a 0.90+ strategy. (~1% of series carry extra maker fees; ignored here.)
FEE_COEF = 0.07


def _get(url: str, tries: int = 6, pause: float = 0.18) -> dict:
    """GET with polite throttle + 429 backoff. Returns dict, or {'_err': code}."""
    time.sleep(pause)
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(1.5 * (i + 1))
                continue
            return {"_err": e.code}
        except Exception as e:  # noqa: BLE001
            return {"_err": str(e)}
    return {"_err": "429-giveup"}


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _ts(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _is_parlay(t: str) -> bool:
    return any(x in t for x in ("MVE", "MULTI", "MULTIGAME"))


# ----------------------------- discovery -----------------------------------

def discover_liquid_series(freqs, categories, probe_limit=60):
    """List recurring short-dated non-parlay series and rank by aggregate OI."""
    d = _get(f"{BASE}/series")
    allser = d.get("series", [])
    cand = [s for s in allser
            if not _is_parlay(s["ticker"])
            and (s.get("frequency") or "").lower() in freqs
            and (not categories or s.get("category") in categories)]
    print(f"[discover] {len(allser)} series -> {len(cand)} recurring short-dated candidates",
          file=sys.stderr)
    ranked = []
    for i, s in enumerate(cand):
        tk = s["ticker"]
        d = _get(f"{BASE}/markets?limit={probe_limit}&status=settled&series_ticker={tk}")
        ms = d.get("markets", [])
        oi = sum(_f(m.get("open_interest_fp"), 0.0) for m in ms)
        if ms:
            ranked.append({"series": tk, "category": s.get("category"),
                           "frequency": s.get("frequency"), "oi": oi,
                           "n_settled": len(ms)})
        if (i + 1) % 50 == 0:
            print(f"[discover] probed {i+1}/{len(cand)} ...", file=sys.stderr)
    ranked.sort(key=lambda r: -r["oi"])
    os.makedirs(DATA, exist_ok=True)
    with open(CACHE, "w") as f:
        json.dump(ranked, f, indent=1)
    print(f"[discover] {len(ranked)} series with settled history -> {CACHE}", file=sys.stderr)
    print("[discover] top 15 by open interest:", file=sys.stderr)
    for r in ranked[:15]:
        print(f"    {r['series']:22s} OI={r['oi']:14,.0f}  ({r['category']}/{r['frequency']})",
              file=sys.stderr)
    return ranked


def load_universe(args):
    if args.series:
        return [s.strip() for s in args.series.split(",") if s.strip()]
    if args.discover or not os.path.exists(CACHE):
        ranked = discover_liquid_series(
            {f.strip() for f in args.freqs.split(",")},
            {c.strip() for c in args.categories.split(",")} if args.categories else None,
            args.probe_limit)
    else:
        with open(CACHE) as f:
            ranked = json.load(f)
    return [r["series"] for r in ranked[:args.top]]


# ----------------------------- pull + analyze ------------------------------

def iter_settled_markets(series: str, max_markets: int):
    cursor, got = None, 0
    while got < max_markets:
        url = f"{BASE}/markets?limit=200&status=settled&series_ticker={series}"
        if cursor:
            url += f"&cursor={cursor}"
        d = _get(url)
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


def snapshot_at_horizon(series, ticker, close_ts, horizon_h):
    """(yes_ask, yes_bid, vol) at ~horizon_h before close, from candlesticks; or None."""
    target = close_ts - int(horizon_h * 3600)
    url = (f"{BASE}/series/{series}/markets/{ticker}/candlesticks"
           f"?start_ts={target - 6*3600}&end_ts={close_ts}&period_interval=60")
    d = _get(url)
    if "_err" in d:
        return None
    candles = d.get("candlesticks", [])
    pick = None
    for c in candles:
        if c.get("end_period_ts", 0) <= target:
            pick = c
        else:
            break
    if pick is None:
        pick = candles[0] if candles else None
    if pick is None:
        return None
    return (_f((pick.get("yes_ask") or {}).get("close_dollars")),
            _f((pick.get("yes_bid") or {}).get("close_dollars")),
            _f(pick.get("volume_fp"), 0.0))


def fee_per_contract(price: float) -> float:
    return FEE_COEF * price * (1.0 - price)


def collect_rows(series_list, horizons, threshold, max_markets, min_vol):
    """Pull each market's candles once per horizon; build qualifying bet rows."""
    rows = []
    stats = {"markets": 0, "with_candles": 0, "parlay_skipped": 0}
    for si, series in enumerate(series_list):
        print(f"[sweep {si+1}/{len(series_list)}] {series} (rows so far={len(rows)})",
              file=sys.stderr)
        for m in iter_settled_markets(series, max_markets):
            tick = m["ticker"]
            if _is_parlay(tick):
                stats["parlay_skipped"] += 1
                continue
            result = m.get("result")
            if result not in ("yes", "no"):
                continue
            stats["markets"] += 1
            close_ts = _ts(m["close_time"])
            seen_candle = False
            for H in horizons:
                snap = snapshot_at_horizon(series, tick, close_ts, H)
                if not snap:
                    continue
                yes_ask, yes_bid, vol = snap
                if yes_ask is None or yes_bid is None:
                    continue
                seen_candle = True
                no_ask = 1.0 - yes_bid
                if yes_ask >= threshold:
                    side, price, won = "yes", yes_ask, (result == "yes")
                elif no_ask >= threshold:
                    side, price, won = "no", no_ask, (result == "no")
                else:
                    continue
                if price >= 1.0 or vol < min_vol:
                    continue
                fee = fee_per_contract(price)
                net_pnl = (1.0 if won else 0.0) - price - fee
                rows.append({"series": series, "ticker": tick, "result": result,
                             "side": side, "horizon_h": H, "entry_price": round(price, 4),
                             "fee": round(fee, 5), "won": won,
                             "net_pnl": round(net_pnl, 5),
                             "net_ret": round(net_pnl / price, 5), "vol_at_snap": vol})
            if seen_candle:
                stats["with_candles"] += 1
    return rows, stats


# ----------------------------- report --------------------------------------

def report(rows, stats, horizons, threshold, min_vol, out_path):
    print("\n" + "=" * 70)
    print("KALSHI 'near-certain favorite' sweep — liquid universe")
    print(f"  horizons={horizons}h before close | buy favorite ask >= {threshold} "
          f"| min snap vol={min_vol}")
    print("=" * 70)
    print(f"settled single markets scanned : {stats['markets']}")
    print(f"  with usable candle history   : {stats['with_candles']}")
    print(f"  parlay (MVE) markets skipped : {stats['parlay_skipped']}")
    print(f"QUALIFYING bets (>= threshold) : {len(rows)}")
    if not rows:
        print("\nNo qualifying markets — widen --top / --max-markets or lower --threshold.")
        return
    if out_path:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        print(f"[out] wrote {len(rows)} rows -> {out_path}")

    for H in horizons:
        g = [r for r in rows if r["horizon_h"] == H]
        if g:
            _block(g, f"HORIZON {H}h")
    print("\nCAVEATS: candle ask captures spread but not depth/partial fills; fee = published "
          "\ngeneral quadratic formula (per-contract limit; ~1% of series add maker fees); "
          "\nt-stat assumes independent bets (resolutions cluster -> OPTIMISTIC). Coverage "
          "\nprobe, not a validated edge — heed the drop-top-N stress test before any capital.")


def _block(g, title):
    n = len(g)
    wins = sum(r["won"] for r in g)
    mean_price = sum(r["entry_price"] for r in g) / n
    win_rate = wins / n
    mean_pnl = sum(r["net_pnl"] for r in g) / n
    mean_ret = sum(r["net_ret"] for r in g) / n
    losses = [r for r in g if not r["won"]]
    print(f"\n--- {title}: n={n} ---")
    print(f"  mean entry (implied p)   : {mean_price:.3f}")
    print(f"  realized favorite win    : {win_rate:.3f} ({wins}/{n})")
    print(f"  calibration edge         : {win_rate-mean_price:+.3f} "
          f"({'UNDERpriced (good)' if win_rate-mean_price>0 else 'OVERpriced/efficient'})")
    print(f"  mean net P&L / contract  : {mean_pnl:+.4f}")
    print(f"  mean net return on stake : {mean_ret:+.4%}")
    print(f"  losses                   : {len(losses)}/{n} ({len(losses)/n:.1%}), "
          f"worst {min(r['net_ret'] for r in g):.1%}")
    if n > 1:
        sd = (sum((r["net_ret"] - mean_ret) ** 2 for r in g) / (n - 1)) ** 0.5
        t = mean_ret / (sd / math.sqrt(n)) if sd > 0 else float("nan")
        print(f"  naive t-stat (optimistic): {t:.2f}")
    # drop-top-N-winners stress test (survivorship tripwire)
    for k in (1, 5):
        if n > k:
            trimmed = sorted(g, key=lambda r: r["net_ret"])[:-k]
            mr = sum(r["net_ret"] for r in trimmed) / len(trimmed)
            print(f"  drop top {k} winners      : net return {mr:+.4%}")
    # price buckets
    buckets = {}
    for r in g:
        b = min(int(r["entry_price"] * 20) / 20, 0.95)
        buckets.setdefault(b, []).append(r)
    print("  by price bucket:")
    for b in sorted(buckets):
        gb = buckets[b]
        print(f"    [{b:.2f}-{b+0.05:.2f}) n={len(gb):4d} "
              f"win={sum(x['won'] for x in gb)/len(gb):.3f} "
              f"net_ret={sum(x['net_ret'] for x in gb)/len(gb):+.2%}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--discover", action="store_true", help="(re)build the liquid-series cache")
    ap.add_argument("--series", default=None, help="explicit comma list (overrides universe)")
    ap.add_argument("--top", type=int, default=50, help="top-N liquid series to sweep")
    ap.add_argument("--freqs", default=",".join(sorted(SHORT_FREQS)),
                    help="frequencies treated as short-dated for discovery")
    ap.add_argument("--categories", default=None, help="restrict discovery to these categories")
    ap.add_argument("--probe-limit", type=int, default=60, help="markets sampled per series to rank OI")
    ap.add_argument("--horizon-hours", default="24", help="comma list of hours before close")
    ap.add_argument("--threshold", type=float, default=0.90)
    ap.add_argument("--max-markets", type=int, default=120, help="max settled markets per series")
    ap.add_argument("--min-vol", type=float, default=0.0, help="min snapshot candle volume")
    ap.add_argument("--out", default=None, help="JSONL path for qualifying rows")
    a = ap.parse_args()

    horizons = [float(x) for x in a.horizon_hours.split(",") if x.strip()]
    if a.discover and not a.series:
        discover_liquid_series({f.strip() for f in a.freqs.split(",")},
                               {c.strip() for c in a.categories.split(",")} if a.categories else None,
                               a.probe_limit)
        if not os.path.exists(CACHE):
            return
    universe = load_universe(a)
    print(f"[universe] sweeping {len(universe)} series at horizons {horizons}h", file=sys.stderr)
    rows, stats = collect_rows(universe, horizons, a.threshold, a.max_markets, a.min_vol)
    report(rows, stats, horizons, a.threshold, a.min_vol, a.out)


if __name__ == "__main__":
    main()
