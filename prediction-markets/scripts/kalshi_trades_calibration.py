#!/usr/bin/env python3
"""
kalshi_trades_calibration.py — validate the taker-edge finding on REAL fills at scale.

Our candle-based map (kalshi_calibration.py, docs/kalshi-calibration-FINDINGS.md) found no
taker edge on ~2,460 synthetic ask-aware trades. This re-runs the same question on MILLIONS
of REAL executed trades from the open-source TrevorJS/kalshi-trades dataset (Hugging Face,
public, ~160M trades; we use one 10M-trade shard joined to market outcomes -> ~9.5M settled
trades). Every trade has the price the taker actually paid, the side they took, and the
market's realized result, so this measures the taker's realized return directly — and the
maker's edge is its mirror.

Per trade: p = (yes_price if taker_side='yes' else no_price)/100 ; won = (taker_side==result)
           fee = 0.07*p*(1-p) ; net = (1 if won else 0) - p - fee ; net_ret = net/p
Contract-weighted by `count` for the economic numbers; per-trade for the t-stat (flagged
optimistic — trades cluster within a market, so true significance is lower).

BUILD the joined sample (one-time, ~80s, ~1.6GB streamed -> 117MB local) via duckdb httpfs:
  see the COPY in the project notes; default path below.

USAGE:
  python3 prediction-markets/scripts/kalshi_trades_calibration.py \
      --joined prediction-markets/data/trevorjs/joined_t0.parquet

Public data, MIT-licensed source. Needs duckdb (`pip install duckdb`).
"""
from __future__ import annotations
import argparse, os, sys

try:
    import duckdb
except ImportError:
    sys.exit("needs duckdb: python3 -m pip install duckdb")

FEE = 0.07


def per_trade_cte(joined, fee):
    """SQL CTE projecting each trade to (p, won, net, net_ret, count, ...)."""
    return f"""
    WITH base AS (
      SELECT
        CASE WHEN taker_side='yes' THEN yes_price ELSE no_price END / 100.0 AS p,
        (taker_side = result)::INT AS won,
        taker_side, market_type, count AS c
      FROM read_parquet('{joined}')
      WHERE (CASE WHEN taker_side='yes' THEN yes_price ELSE no_price END) BETWEEN 1 AND 99
    ),
    t AS (
      SELECT *,
        {fee}*p*(1-p) AS fee,
        won - p - {fee}*p*(1-p) AS net,
        (won - p - {fee}*p*(1-p))/p AS net_ret
      FROM base
    )
    """


def headline(con, joined, fee):
    q = per_trade_cte(joined, fee) + """
    SELECT count(*) n_trades, sum(c) contracts, sum(c*p) notional,
           sum(c*net) total_net,
           sum(c*net)/sum(c) net_per_contract,
           sum(c*net)/sum(c*p) ret_on_notional
    FROM t
    """
    n, ctr, notion, tot, npc, ron = con.execute(q).fetchone()
    print("=" * 76)
    print("KALSHI TAKER REALIZED RETURN — real fills (TrevorJS/kalshi-trades, 1 shard)")
    print("=" * 76)
    print(f"settled trades          : {n:,}")
    print(f"contracts (sum count)   : {ctr:,.0f}")
    print(f"taker notional ($)      : {notion:,.0f}")
    print(f"taker total net P&L ($) : {tot:,.0f}")
    print(f"taker net P&L / contract: {npc:+.4f}")
    print(f"taker return on notional: {ron:+.3%}   <-- takers' loss (includes their fee to Kalshi)")
    print(f"=> The maker does NOT capture all of this: ~2.56% of premium is takers' fee to KALSHI,")
    print(f"   not the maker. For the CORRECT pre-fee maker edge (+3.50% gross / +2.86% net),")
    print(f"   see kalshi_maker_ev.py — do NOT read -(taker_net) as the maker edge.")


def by_bucket(con, joined, fee):
    q = per_trade_cte(joined, fee) + """
    SELECT floor(p*20)/20 AS bkt, count(*) n, sum(c) ctr,
           sum(c*p)/sum(c) mean_px,
           sum(c*won)/sum(c) win,
           sum(c*net_ret)/sum(c) cw_net_ret,
           avg(net_ret) ew_net_ret, stddev_samp(net_ret) sd
    FROM t GROUP BY 1 ORDER BY 1
    """
    print("\n--- taker realized return by price paid (the calibration curve) ---")
    print(f"  {'bucket':>13} {'n_trades':>11} {'mean_px':>8} {'win':>6} "
          f"{'calib':>8} {'taker_net':>10} {'t':>9}")
    for bkt, n, ctr, mpx, win, cwret, ewret, sd in con.execute(q).fetchall():
        t = (ewret / (sd / (n ** 0.5))) if sd and n > 1 else float("nan")
        flag = "  <== taker +EV?" if cwret > 0 else ""
        print(f"  [{bkt:.2f}-{bkt+0.05:.2f}) {n:11,} {mpx:8.3f} {win:6.3f} "
              f"{win-mpx:+8.3f} {cwret:+10.2%} {t:9.0f}{flag}")
    print("  (t from per-trade variance — OPTIMISTIC: trades cluster within a market.)")


def by_side(con, joined, fee):
    q = per_trade_cte(joined, fee) + """
    SELECT taker_side, count(*) n, sum(c) ctr,
           sum(c*p)/sum(c) mean_px, sum(c*won)/sum(c) win, sum(c*net_ret)/sum(c) cw_net_ret
    FROM t GROUP BY 1 ORDER BY 1
    """
    print("\n--- by taker side (who overpays) ---")
    for side, n, ctr, mpx, win, cwret in con.execute(q).fetchall():
        print(f"  taker={side:3s}  n={n:11,}  mean_px={mpx:.3f}  win={win:.3f}  "
              f"taker_net={cwret:+.2%}")


def by_series_fade(con, joined, fee):
    """Where the cheap-YES-longshot fade is richest: total $ takers lose buying cheap YES,
    by series (= the maker's $ fade opportunity)."""
    q = f"""
    WITH base AS (
      SELECT regexp_extract(ticker,'^[A-Z]+',0) series, yes_price/100.0 p,
             (taker_side=result)::INT won, count AS c
      FROM read_parquet('{joined}')
      WHERE taker_side='yes' AND yes_price BETWEEN 1 AND 15
    ), t AS (SELECT *, won - p - {fee}*p*(1-p) AS net FROM base)
    SELECT series, count(*) n, sum(c) ctr, sum(c*p) notional,
           sum(c*net) total_net, sum(c*net)/sum(c*p) ret
    FROM t WHERE series<>'' GROUP BY 1 HAVING sum(c*p) > 20000
    ORDER BY total_net ASC LIMIT 12
    """
    print("\n--- WHERE: cheap-YES-longshot fade by series (taker $ lost = maker $ opportunity) ---")
    print(f"  {'series':22s} {'n':>9} {'notional$':>11} {'taker_net$':>11} {'ret':>8}")
    for series, n, ctr, notion, tot, ret in con.execute(q).fetchall():
        print(f"  {series:22s} {n:9,} {notion:11,.0f} {tot:11,.0f} {ret:+8.1%}")


def by_time_to_close(con, joined, fee):
    """WHEN the cheap-YES-longshot overpricing exists — the make-or-break maker question.
    If takers lose at >6h/>1d to close, a resting maker can harvest it; if only <1h, untradable."""
    q = f"""
    WITH base AS (
      SELECT (epoch(close_time)-epoch(created_time))/3600.0 h2c,
             yes_price/100.0 p, (taker_side=result)::INT won, count AS c
      FROM read_parquet('{joined}')
      WHERE taker_side='yes' AND yes_price BETWEEN 1 AND 15 AND close_time IS NOT NULL
    ), t AS (
      SELECT *, won - p - {fee}*p*(1-p) AS net,
        CASE WHEN h2c<1 THEN '0: <1h' WHEN h2c<6 THEN '1: 1-6h' WHEN h2c<24 THEN '2: 6-24h'
             WHEN h2c<72 THEN '3: 1-3d' ELSE '4: >3d' END bkt
      FROM base WHERE h2c>=0
    )
    SELECT bkt, count(*) n, sum(c) ctr, sum(c*p) notional,
           sum(c*won)/sum(c) win, sum(c*net)/sum(c*p) taker_ret
    FROM t GROUP BY 1 ORDER BY 1
    """
    print("\n--- WHEN: cheap-YES-longshot taker return by time-to-close (maker harvestability) ---")
    print(f"  {'time to close':14s} {'n':>9} {'notional$':>11} {'win':>6} {'taker_ret':>10} {'maker_edge':>11}")
    for bkt, n, ctr, notion, win, tret in con.execute(q).fetchall():
        print(f"  {bkt:14s} {n:9,} {notion:11,.0f} {win:6.3f} {tret:+10.1%} {-tret:+11.1%}")
    print("  maker_edge = -taker_ret (GROSS). Harvestable only where it's large at >1h/>6h to close.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--joined", default="prediction-markets/data/trevorjs/joined_t0.parquet",
                    help="local joined trades+outcomes parquet")
    ap.add_argument("--fee-coef", type=float, default=FEE)
    a = ap.parse_args()
    if not os.path.exists(a.joined):
        sys.exit(f"missing {a.joined} — build the joined sample first (see module docstring)")
    con = duckdb.connect()
    headline(con, a.joined, a.fee_coef)
    by_bucket(con, a.joined, a.fee_coef)
    by_side(con, a.joined, a.fee_coef)
    by_series_fade(con, a.joined, a.fee_coef)
    by_time_to_close(con, a.joined, a.fee_coef)
    print("\nNOTE: these are REAL fills at the moment of trade (any time in market life), NOT a")
    print("fixed horizon. The cheap-longshot loss (the optimism tax) is huge and monotonic ->")
    print("the maker longshot-fade is where the edge concentrates. The favorite band [0.85-1.00)")
    print("looking taker-+EV is a TIMING/SELECTION effect (real takers buy favorites late, tight-")
    print("spread, near-resolved) — it does NOT contradict the fixed-horizon ask-aware result that")
    print("buying favorites 24h-out is -EV (kalshi-near-certain-favorite-FINDINGS.md). For a STRATEGY")
    print("the disciplined fixed-horizon backtest governs; this is the realized cross-section.")


if __name__ == "__main__":
    main()
