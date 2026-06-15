#!/usr/bin/env python3
"""
kalshi_maker_ev.py — Phase-A maker EV model for the longshot-fade, with CORRECT accounting.

A maker who fades (sells) a cheap-YES longshot at price p collects p and pays $1 if it hits:
    maker pre-fee P&L / contract = p - won           (won = 1 if YES resolves)
    maker net   P&L / contract   = p - won - maker_fee   (maker_fee = 0.0175*p*(1-p))
The taker's LOSS is NOT the maker's gain: the taker also pays a fee to KALSHI
(taker_fee = 0.07*p*(1-p)), which at low prices is ~6.6% of premium. So the maker only
captures the PRE-FEE mispricing transfer, never the taker's fee. (An earlier draft that set
maker_edge = -(taker_net) double-counted that fee — this script is the correction.)

It decomposes, overall and by time-to-close x sports/non-sports, on the 9.5M-trade joined
sample: premium, taker net, the fee component, and the CORRECT maker gross/net edge — then
bounds realizable EV against the L2-observed queue depth. NOT a tick simulation; an
explicit-assumption EV model. See docs/kalshi-calibration-FINDINGS.md.

USAGE:
  python3 prediction-markets/scripts/kalshi_maker_ev.py \
      --joined prediction-markets/data/trevorjs/joined_t0.parquet \
      --l2 prediction-markets/data/l2/collect_20260614.jsonl
"""
from __future__ import annotations
import argparse, sys
try:
    import duckdb
except ImportError:
    sys.exit("needs duckdb: python3 -m pip install duckdb")

TAKER_FEE, MAKER_FEE = 0.07, 0.0175
SHARDS = 16  # trades-0000 is 1 of 16 shards
SPORTS = "NFL|NCAA|EPL|MLB|NBA|NHL|WNBA|UFC|SOCCER|GAME|SPREAD|TOTAL|MULTIGAME|SPORTS|TENNIS|GOLF|NASCAR"


def decomp_select():
    return f"""
      sum(c*p) premium, sum(c*won) payout,
      sum(c*{TAKER_FEE}*p*(1-p)) taker_fee, sum(c*{MAKER_FEE}*p*(1-p)) maker_fee,
      (sum(c*won)-sum(c*p)-sum(c*{TAKER_FEE}*p*(1-p)))/sum(c*p) taker_net_ret,
      (sum(c*p)-sum(c*won))/sum(c*p) maker_gross_ret,
      (sum(c*p)-sum(c*won)-sum(c*{MAKER_FEE}*p*(1-p)))/sum(c*p) maker_net_ret
    """


def overall(con, J):
    q = f"""
    WITH t AS (SELECT (CASE WHEN taker_side='yes' THEN yes_price ELSE no_price END)/100.0 p,
                      (taker_side=result)::INT won, count AS c
               FROM read_parquet('{J}')
               WHERE (CASE WHEN taker_side='yes' THEN yes_price ELSE no_price END) BETWEEN 1 AND 99)
    SELECT {decomp_select()} FROM t
    """
    prem, pay, tf, mf, tnet, mg, mn = con.execute(q).fetchone()
    print("=" * 78)
    print("CORRECTED maker accounting — ALL taker trades (fixes the earlier '+6% ceiling')")
    print("=" * 78)
    print(f"  premium $        : {prem:,.0f}")
    print(f"  taker net return : {tnet:+.2%}   (what takers lose, incl. their fee to Kalshi)")
    print(f"  of which fee->Kalshi: {tf/prem:+.2%} of premium  (NOT captured by the maker)")
    print(f"  MAKER gross edge : {mg:+.2%}   <- the real pre-fee transfer the maker captures")
    print(f"  MAKER net edge   : {mn:+.2%}   (after the {MAKER_FEE} maker fee)")
    print(f"  => the maker captures ~{mg:+.1%}, not -(taker_net). The rest of takers' loss is fees.")


def by_zone(con, J):
    q = f"""
    WITH b AS (
      SELECT (epoch(close_time)-epoch(created_time))/3600.0 h2c, yes_price/100.0 p,
             (result='yes')::INT won, count AS c,
             regexp_matches(regexp_extract(ticker,'^[A-Z]+',0), '{SPORTS}') is_sport
      FROM read_parquet('{J}')
      WHERE taker_side='yes' AND yes_price BETWEEN 1 AND 15 AND close_time IS NOT NULL
    ),
    t AS (SELECT *, CASE WHEN h2c<1 THEN '0 <1h' WHEN h2c<6 THEN '1 1-6h' WHEN h2c<24 THEN '2 6-24h'
                        WHEN h2c<72 THEN '3 1-3d' ELSE '4 >3d' END z
          FROM b WHERE h2c>=0)
    SELECT z, (NOT is_sport) nonsport, {decomp_select()}, sum(c) ctr
    FROM t GROUP BY 1,2 ORDER BY 1,2
    """
    print("\n--- cheap-YES-longshot MAKER edge by zone x non-sports (CORRECT pre-fee transfer) ---")
    print(f"  {'zone':8s} {'grp':10s} {'premium$':>11} {'taker_net':>10} "
          f"{'maker_gross':>11} {'maker_net':>10}")
    rows = con.execute(q).fetchall()
    for z, nonsport, prem, pay, tf, mf, tnet, mg, mn, ctr in rows:
        grp = "non-sport" if nonsport else "SPORTS"
        print(f"  {z:8s} {grp:10s} {prem:11,.0f} {tnet:+10.1%} {mg:+11.1%} {mn:+10.1%}")
    print("  maker_gross = pre-fee transfer (real edge); maker_net = after maker fee.")
    return rows


def ev_model(con, J, rows, l2):
    # year span of the shard, for annualizing volume
    yrs = con.execute(f"SELECT date_diff('day',min(created_time),max(created_time))/365.25 "
                      f"FROM read_parquet('{J}')").fetchone()[0]
    # the calm, accessible zone: 6-24h, non-sports
    calm = next((r for r in rows if r[0] == '2 6-24h' and r[1]), None)
    print("\n" + "=" * 78)
    print("PHASE-A EV — the accessible 'calm zone' (6-24h to close, NON-sports)")
    print("=" * 78)
    if not calm:
        print("  no calm-zone rows."); return
    prem, mg, mn = calm[2], calm[7], calm[8]
    annual_prem = prem / yrs * SHARDS
    print(f"  shard premium (calm)   : ${prem:,.0f}  over {yrs:.1f}y  -> annualized x{SHARDS} shards: ${annual_prem:,.0f}/yr")
    print(f"  maker GROSS edge       : {mg:+.2%} of premium")
    print(f"  maker NET edge         : {mn:+.2%} of premium (after maker fee)")
    # L2 queue depth in the calm zone (non-sports cheap longshot, 6-24h to close)
    qd = con.execute(f"""
      SELECT avg(yes_ask_sz), median(yes_ask_sz), count(distinct ticker)
      FROM read_json_auto('{l2}')
      WHERE yes_ask BETWEEN 0.01 AND 0.15
        AND NOT regexp_matches(regexp_extract(ticker,'^[A-Z]+',0), '{SPORTS}')
        AND (epoch(close::TIMESTAMP)-ts)/3600.0 BETWEEN 6 AND 24
    """).fetchone()
    print(f"  L2 queue ahead (calm)  : mean {qd[0] or 0:,.0f} / median {qd[1] or 0:,.0f} contracts "
          f"resting on the ask, across {qd[2]} markets")
    print("\n  Realizable EV = capture_fraction x annual_premium x maker_NET_edge:")
    if mn <= 0:
        print(f"  *** maker NET edge is {mn:+.2%} (<=0): NO edge to capture in the calm zone. ***")
        print("  The full annual premium at any capture fraction yields <= $0. Fading cheap")
        print("  longshots 6-24h out is not +EV before you even reach the queue.")
    else:
        for cf in (0.02, 0.05, 0.10, 0.25):
            print(f"    capture {cf:4.0%}: ${cf*annual_prem*mn:,.0f}/yr net")
        print("  (capture is bounded HARD by the queue above — you fill only flow beyond it.)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--joined", required=True)
    ap.add_argument("--l2", required=True)
    a = ap.parse_args()
    con = duckdb.connect()
    overall(con, a.joined)
    rows = by_zone(con, a.joined)
    ev_model(con, a.joined, rows, a.l2)


if __name__ == "__main__":
    main()
