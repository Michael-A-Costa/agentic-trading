#!/usr/bin/env python3
"""
kalshi_sports_scope.py — scope the ONE big maker edge: final-hours SPORTS cheap-longshot fade.

The maker EV model (kalshi_maker_ev.py) found the large edge (+42% net) lives in final-hours
sports — but flagged it as "inaccessible (HFT)". This sizes that prize and exposes its
structure so we can decide if it's worth an HFT build:
  - the TAM (annualized taker premium + total maker $ pool at the realized edge),
  - HOW FAST the edge is (sub-hour timing — last 15min? last hour?),
  - the tail risk (how often the longshot hits = maker pays $1; correlated-upset exposure),
  - which sports.

Correct pre-fee maker accounting: maker_net = p - won - 0.0175*p*(1-p) per contract (the taker
fee goes to Kalshi, NOT the maker). On the 9.5M-trade joined sample; x16 shards to annualize.

USAGE:
  python3 prediction-markets/scripts/kalshi_sports_scope.py \
      --joined prediction-markets/data/trevorjs/joined_t0.parquet
"""
from __future__ import annotations
import argparse, sys
try:
    import duckdb
except ImportError:
    sys.exit("needs duckdb: python3 -m pip install duckdb")

MAKER_FEE = 0.0175
SHARDS = 16
SPORTS = "NFL|NCAA|EPL|MLB|NBA|NHL|WNBA|UFC|SOCCER|GAME|SPREAD|TOTAL|MULTIGAME|SPORTS|TENNIS|GOLF|NASCAR"

BASE = f"""
  WITH b AS (
    SELECT (epoch(close_time)-epoch(created_time))/3600.0 h2c, yes_price/100.0 p,
           (result='yes')::INT won, count AS c, regexp_extract(ticker,'^[A-Z]+',0) series
    FROM read_parquet('{{J}}')
    WHERE taker_side='yes' AND yes_price BETWEEN 1 AND 15 AND close_time IS NOT NULL
      AND regexp_matches(regexp_extract(ticker,'^[A-Z]+',0), '{SPORTS}')
  ),
  t AS (SELECT *, (p - won - {MAKER_FEE}*p*(1-p)) mknet FROM b WHERE h2c>=0)
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--joined", required=True)
    a = ap.parse_args()
    con = duckdb.connect()
    J = a.joined
    yrs = con.execute(f"SELECT date_diff('day',min(created_time),max(created_time))/365.25 "
                      f"FROM read_parquet('{J}')").fetchone()[0]
    scale = SHARDS / yrs
    print("=" * 74)
    print("FINAL-HOURS SPORTS cheap-longshot fade — opportunity scope")
    print(f"(1 shard over {yrs:.1f}y; annualized x{SHARDS} shards => x{scale:.1f})")
    print("=" * 74)

    # TAM
    tot = con.execute(BASE.format(J=J) + """
      SELECT sum(c*p) premium, sum(c*mknet) maker_pool, sum(c*won)/sum(c) hit_rate, count(*) n
      FROM t WHERE h2c < 6
    """).fetchone()
    prem, pool, hit, n = tot
    print(f"\nfinal-6h sports cheap-longshot premium : ${prem:,.0f}/shard -> ${prem*scale:,.0f}/yr")
    print(f"TOTAL maker $ pool (net, ALL makers)   : ${pool:,.0f}/shard -> ${pool*scale:,.0f}/yr")
    print(f"longshot HIT rate (maker pays $1)       : {hit:.3%}  (so maker wins {1-hit:.1%} of fills)")
    print("  ^ this pool is split across ALL makers (Jump/SIG/etc.); a solo entrant captures a slice.")

    # how fast — sub-hour timing
    print("\n--- HOW FAST is the edge? (maker net by time-to-close) ---")
    print(f"  {'window':12s} {'n':>10} {'premium$':>12} {'maker_net':>10} {'hit%':>7}")
    rows = con.execute(BASE.format(J=J) + """
      SELECT CASE WHEN h2c<0.25 THEN '0 <15min' WHEN h2c<1 THEN '1 15-60min'
                  WHEN h2c<3 THEN '2 1-3h' WHEN h2c<6 THEN '3 3-6h' ELSE '4 6h+' END w,
             count(*) n, sum(c*p) prem, sum(c*mknet)/sum(c*p) mknet_ret, sum(c*won)/sum(c) hit
      FROM t GROUP BY 1 ORDER BY 1
    """).fetchall()
    for w, nn, pr, mr, h in rows:
        print(f"  {w:12s} {nn:10,} {pr:12,.0f} {mr:+10.1%} {h:7.2%}")

    # which sports
    print("\n--- which sports (final-6h, by maker $ pool) ---")
    print(f"  {'series':14s} {'premium$/yr':>13} {'maker$/yr':>12} {'net_edge':>9} {'hit%':>7}")
    rows = con.execute(BASE.format(J=J) + """
      SELECT series, sum(c*p)*16 premy, sum(c*mknet)*16 mky, sum(c*mknet)/sum(c*p) edge,
             sum(c*won)/sum(c) hit
      FROM t WHERE h2c<6 GROUP BY 1 HAVING sum(c*p)>5000 ORDER BY mky DESC LIMIT 10
    """).fetchall()
    for s, pr, mk, e, h in rows:
        sc = scale / SHARDS  # already x16 above; convert to true annual
        print(f"  {s:14s} {pr*sc:13,.0f} {mk*sc:12,.0f} {e:+9.1%} {h:7.2%}")

    # tail risk
    print("\n--- TAIL RISK: when the longshot hits, the maker pays $1 ---")
    tail = con.execute(BASE.format(J=J) + """
      SELECT sum(CASE WHEN won=1 THEN c*(1-p) ELSE 0 END) gross_loss_on_hits,
             sum(CASE WHEN won=0 THEN c*p ELSE 0 END) gross_prem_on_wins
      FROM t WHERE h2c<6
    """).fetchone()
    print(f"  $ paid out on losing fills (hits)   : ${tail[0]:,.0f}/shard")
    print(f"  $ premium kept on winning fills     : ${tail[1]:,.0f}/shard")
    print("  net is positive ON AVERAGE, but each hit is ~20x the premium collected — selling")
    print("  cheap longshots is short-vol: correlated upsets (a chalk-heavy slate all hitting)")
    print("  are the blow-up mode. Position/inventory caps are mandatory.")


if __name__ == "__main__":
    main()
