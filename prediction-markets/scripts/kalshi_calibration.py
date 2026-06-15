#!/usr/bin/env python3
"""
kalshi_calibration.py — full TAKER calibration / mispricing map across the liquid
Kalshi universe. Generalizes kalshi_pull.py beyond the >=0.90 favorite band.

For every settled market, at horizon H, it prices BOTH taker trades you could actually
put on at the ask:
  - buy YES @ yes_ask
  - buy NO  @ no_ask = 1 - yes_bid
records the realized outcome and net P&L net of the published fee, and bins by the
PRICE YOU PAY across the whole [0,1] spectrum, by category and horizon.

QUESTION IT ANSWERS:
  Is there ANY price region / category where buying at the ask is +EV net of fees on
  Kalshi — i.e. a taker-harvestable mispricing — or is the whole curve efficient/rich?
  If rich everywhere => only a MAKER who captures the spread has a shot, and that needs
  forward L2 collection (a separate build), NOT this historical-candle backtest.

Discipline (same as kalshi_pull.py): ask-aware (never mid) fills, net of fee, drop-top-N
stress test, naive t flagged optimistic (resolutions cluster). Read-only, public data,
stdlib-only. Reuses the discovery cache built by kalshi_pull.py.

USAGE:
  python3 prediction-markets/scripts/kalshi_calibration.py --top 30 --horizon-hours 24 \
      --out prediction-markets/data/calibration.jsonl
"""
from __future__ import annotations
import argparse, json, math, os, sys
from collections import defaultdict

# reuse the low-level plumbing from the sibling probe (ensure its dir is importable
# regardless of CWD)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kalshi_pull import (CACHE, _ts, _is_parlay,
                         iter_settled_markets, snapshot_at_horizon, fee_per_contract)


def load_series(top, skip=0, category=None):
    if not os.path.exists(CACHE):
        sys.exit("no discovery cache — run `kalshi_pull.py --discover` first")
    with open(CACHE) as f:
        ranked = json.load(f)
    cat = {r["series"]: r.get("category") for r in ranked}  # keep all for labeling
    if category:
        ranked = [r for r in ranked if category.lower() in (r.get("category") or "").lower()]
    return [r["series"] for r in ranked[skip:skip + top]], cat


def collect(series_list, cat_map, horizons, max_markets, min_vol, out_path):
    rows, stats = [], defaultdict(int)
    out = open(out_path, "w") if out_path else None
    for si, series in enumerate(series_list):
        print(f"[{si+1}/{len(series_list)}] {series} (rows={len(rows)})", file=sys.stderr)
        for m in iter_settled_markets(series, max_markets):
            tick = m["ticker"]
            if _is_parlay(tick):
                stats["parlay"] += 1
                continue
            result = m.get("result")
            if result not in ("yes", "no"):
                continue
            stats["markets"] += 1
            close_ts = _ts(m["close_time"])
            for H in horizons:
                snap = snapshot_at_horizon(series, tick, close_ts, H)
                if not snap:
                    continue
                yes_ask, yes_bid, vol = snap
                if yes_ask is None or yes_bid is None or vol < min_vol:
                    continue
                no_ask = 1.0 - yes_bid
                for side, price, won in (("yes", yes_ask, result == "yes"),
                                         ("no", no_ask, result == "no")):
                    if price is None or price <= 0.0 or price >= 1.0:
                        continue
                    fee = fee_per_contract(price)
                    net = (1.0 if won else 0.0) - price - fee
                    row = {"series": series, "cat": cat_map.get(series), "ticker": tick,
                           "side": side, "H": H, "price": round(price, 4), "won": won,
                           "net": round(net, 5), "net_ret": round(net / price, 5), "vol": vol}
                    rows.append(row)
                    if out:
                        out.write(json.dumps(row) + "\n")
    if out:
        out.close()
        print(f"[out] wrote {len(rows)} rows -> {out_path}", file=sys.stderr)
    return rows, stats


def _bucket_table(rows, title, min_n_flag=30):
    if not rows:
        return []
    buckets = defaultdict(list)
    for r in rows:
        b = min(int(r["price"] * 20) / 20, 0.95)
        buckets[b].append(r)
    print(f"\n=== {title} (n={len(rows)}) ===")
    print(f"  {'bucket':>13} {'n':>6} {'mean_px':>8} {'win':>6} "
          f"{'calib':>8} {'net_ret':>9} {'drop5':>9} {'t':>7}")
    leads = []
    for b in sorted(buckets):
        g = buckets[b]
        m = len(g)
        mpx = sum(r["price"] for r in g) / m
        win = sum(r["won"] for r in g) / m
        nr = sum(r["net_ret"] for r in g) / m
        calib = win - mpx
        if m > 5:
            d5 = sum(r["net_ret"] for r in sorted(g, key=lambda r: r["net_ret"])[:-5]) / (m - 5)
        else:
            d5 = float("nan")
        if m > 1:
            sd = (sum((r["net_ret"] - nr) ** 2 for r in g) / (m - 1)) ** 0.5
            t = nr / (sd / math.sqrt(m)) if sd > 0 else float("nan")
        else:
            t = float("nan")
        # A real candidate must be +EV, well-sampled, robust to drop-top-5, statistically
        # significant (|t|>2), AND not a degenerate near-certain bucket (px in (0.05,0.95) —
        # the 0.95-1.00 / 0.00-0.05 buckets show high t only because variance ~ 0; they're
        # un-tradable pennies, and are the favorite-buy trade FINDINGS already killed).
        lead = (nr > 0 and m >= min_n_flag and d5 > 0 and t > 2.0 and 0.05 < mpx < 0.95)
        flag = "  <== +EV?" if lead else ""
        if lead:
            leads.append((title, b, m, nr, d5, t))
        print(f"  [{b:.2f}-{b+0.05:.2f}) {m:6d} {mpx:8.3f} {win:6.3f} "
              f"{calib:+8.3f} {nr:+9.2%} {d5:+9.2%} {t:7.2f}{flag}")
    return leads


def report(rows, stats, horizons):
    print("\n" + "=" * 78)
    print("KALSHI taker calibration / mispricing map — buy at the ASK, net of fee")
    print(f"  horizons={horizons}h | both sides priced (YES@ask, NO@1-bid) | bin by price paid")
    print("=" * 78)
    print(f"settled single markets scanned : {stats['markets']}  "
          f"(parlay skipped {stats['parlay']})")
    print(f"taker trade-rows priced         : {len(rows)}")
    if not rows:
        print("\nNo rows — widen --top / --max-markets or check connectivity.")
        return

    leads = _bucket_table(rows, "ALL CATEGORIES")

    # per-category, biggest first
    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["cat"] or "?"].append(r)
    for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
        if len(by_cat[cat]) >= 50:
            leads += _bucket_table(by_cat[cat], f"CATEGORY: {cat}")

    print("\n" + "-" * 78)
    if leads:
        print("LEADS — +EV net of fee, n>=30, drop-top-5 robust, |t|>2, px in (0.05,0.95):")
        for (title, b, m, nr, d5, t) in leads:
            print(f"  {title:24s} [{b:.2f}-{b+0.05:.2f})  n={m}  net_ret={nr:+.2%}  "
                  f"drop5={d5:+.2%}  t={t:.2f}")
        print("Candidate TAKER edges — confirm out-of-sample at larger n and the live"
              "\nper-series fee (we tested ~140 buckets, so even these need a fresh sample).")
    else:
        print("NO taker-harvestable pocket found (no bucket +EV after fee, n>=30,"
              "\nsurviving drop-top-5). Implication: the curve is efficient/rich — only a"
              "\nMAKER capturing the spread has a shot, which needs forward L2 collection,"
              "\nnot this historical-candle backtest. See docs/kalshi-market-making-v1-plan.md.")
    print("\nCAVEATS: candle ask captures spread but NOT depth/partial fills; fee = published"
          "\nquadratic (per-contract limit; ~1% of series add maker fees); t assumes independent"
          "\nbets (resolutions cluster -> OPTIMISTIC). Coverage map, not a validated edge.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=30, help="top-N liquid series to sweep")
    ap.add_argument("--skip", type=int, default=0, help="skip the first K series (for OOS slices)")
    ap.add_argument("--category", default=None, help="restrict universe to a category (substring)")
    ap.add_argument("--horizon-hours", default="24", help="comma list of hours before close")
    ap.add_argument("--max-markets", type=int, default=120, help="max settled markets per series")
    ap.add_argument("--min-vol", type=float, default=0.0, help="min snapshot candle volume")
    ap.add_argument("--out", default=None, help="JSONL path for trade rows (kept for reuse)")
    ap.add_argument("--from", dest="from_jsonl", default=None,
                    help="re-report from a saved rows JSONL (no API calls)")
    a = ap.parse_args()
    horizons = [float(x) for x in a.horizon_hours.split(",") if x.strip()]
    if a.from_jsonl:
        with open(a.from_jsonl) as f:
            rows = [json.loads(line) for line in f if line.strip()]
        stats = {"markets": len({r["ticker"] for r in rows}), "parlay": 0}
        report(rows, stats, horizons)
        return
    series_list, cat_map = load_series(a.top, a.skip, a.category)
    print(f"[universe] {len(series_list)} series @ horizons {horizons}h", file=sys.stderr)
    rows, stats = collect(series_list, cat_map, horizons, a.max_markets, a.min_vol, a.out)
    report(rows, stats, horizons)


if __name__ == "__main__":
    main()
