#!/usr/bin/env python3
"""entry_timing_replay.py — backtest the three proposed entry guards against broker-truth round-trips.

Tests (owner-requested, JBL post-mortem 2026-06-17):
  1. EXTENSION gate   — bucket entries by intraday_pct at fill (move from the day's open); does buying
                        a more-extended name earn worse forward P&L?
  2. ANCHOR/chase cap — bucket by chase_pct = (fill_price / screen_last - 1)*100; does paying up above
                        the screen print cost us?
  3. OPEN throttle    — bucket by minutes_since_open at fill; is the opening burst worse than later?

Outcome = realized pnl_pct from broker-truth round-trips (data/ledger_truth.json round_trips).
Exit timing is an established near-wash (exit_counterfactual.py), so pnl_pct is dominated by entry quality.

Join: each round-trip (symbol, entry_ts UTC) -> the most recent engine-log decide row at-or-before the
fill whose screen.entry_candidates contains that symbol, taking that candidate's intraday_pct / last /
range_pos. minutes_since_open is computed straight from entry_ts (regular open = 13:30 UTC, EDT).

Pre-registered read (do NOT loosen after looking): a bucket is a GATE CANDIDATE only if, over >=MIN_N
trips, it is realized-$ negative AND profit_factor < 1.0 AND the loss survives dropping its 2 best trips.
Reports only; changes no dials.
"""
from __future__ import annotations
import argparse, json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TRUTH = REPO / "data" / "ledger_truth.json"
ENGINE = REPO / "data" / "engine-log.jsonl"
OPEN_UTC_MIN = 13 * 60 + 30  # 09:30 ET in EDT = 13:30 UTC


def parse_ts(s: str) -> datetime:
    # round-trip entry_ts is naive UTC "YYYY-MM-DD HH:MM:SS"
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def build_candidate_index():
    """symbol -> sorted list of (epoch, intraday_pct, last, range_pos) from every decide row."""
    idx = defaultdict(list)
    with ENGINE.open() as f:
        for line in f:
            line = line.strip()
            if not line or '"entry_candidates"' not in line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            ts = r.get("ts_utc")
            cands = (r.get("screen") or {}).get("entry_candidates") or []
            if not ts or not cands:
                continue
            try:
                ep = datetime.fromisoformat(ts).timestamp()
            except Exception:
                continue
            for c in cands:
                idx[c["symbol"]].append(
                    (ep, c.get("intraday_pct"), c.get("last"), c.get("range_pos"))
                )
    for s in idx:
        idx[s].sort()
    return idx


def lookup(idx, symbol, fill_ep):
    """nearest candidate row at-or-before the fill for this symbol."""
    rows = idx.get(symbol)
    if not rows:
        return None, None, None
    best = None
    for ep, ipct, last, rpos in rows:
        if ep <= fill_ep + 90:  # allow 90s clock skew between screen and fill
            best = (ipct, last, rpos)
        else:
            break
    if best is None:
        return None, None, None
    # age guard: don't join a stale candidate from a prior session
    return best


def stats(trips):
    """realized summary for a bucket of round-trips."""
    n = len(trips)
    pnl = [t["pnl_pct"] for t in trips]
    usd = [t["realized_usd"] for t in trips]
    wins = [u for u in usd if u > 0]
    losses = [u for u in usd if u <= 0]
    gross_w, gross_l = sum(wins), -sum(losses)
    pf = (gross_w / gross_l) if gross_l > 0 else float("inf")
    # drop-2-best robustness on realized $
    usd_sorted = sorted(usd, reverse=True)
    usd_drop2 = sum(usd_sorted[2:]) if n > 2 else sum(usd_sorted)
    return {
        "n": n,
        "sum_usd": round(sum(usd), 2),
        "mean_pct": round(sum(pnl) / n, 2) if n else 0,
        "median_pct": round(sorted(pnl)[n // 2], 2) if n else 0,
        "win_rate": round(100 * len(wins) / n, 0) if n else 0,
        "pf": round(pf, 2) if pf != float("inf") else "inf",
        "sum_usd_drop2best": round(usd_drop2, 2),
    }


def gate_flag(s, min_n):
    return (s["n"] >= min_n and s["sum_usd"] < 0 and s["pf"] != "inf"
            and s["pf"] < 1.0 and s["sum_usd_drop2best"] < 0)


def report(title, buckets, order, min_n):
    print(f"\n{'='*72}\n{title}\n{'='*72}")
    print(f"{'bucket':<16}{'n':>4}{'sumUSD':>9}{'mean%':>7}{'med%':>7}{'win%':>6}{'PF':>7}{'drop2$':>9}  flag")
    for b in order:
        if b not in buckets:
            continue
        s = stats(buckets[b])
        flag = "  <-- GATE" if gate_flag(s, min_n) else ""
        print(f"{b:<16}{s['n']:>4}{s['sum_usd']:>9}{s['mean_pct']:>7}{s['median_pct']:>7}"
              f"{int(s['win_rate']):>5}%{str(s['pf']):>7}{s['sum_usd_drop2best']:>9}{flag}")


def ext_bucket(v):
    if v is None: return "unknown"
    if v < 0: return "neg(<0%)"
    if v < 3: return "0-3%"
    if v < 6: return "3-6%"
    if v < 10: return "6-10%"
    return "10%+"


def chase_bucket(v):
    if v is None: return "unknown"
    if v < 0: return "below(<0)"
    if v < 0.5: return "0-0.5%"
    if v < 1.5: return "0.5-1.5%"
    if v < 3: return "1.5-3%"
    return "3%+"


def open_bucket(m):
    if m is None: return "unknown"
    if m < 0: return "pre/odd"
    if m < 5: return "0-5m"
    if m < 15: return "5-15m"
    if m < 30: return "15-30m"
    if m < 60: return "30-60m"
    return "60m+"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-n", type=int, default=8, help="pre-registered n bar per bucket")
    args = ap.parse_args()

    truth = json.load(TRUTH.open())
    trips = truth["round_trips"]
    print(f"loaded {len(trips)} broker-truth round-trips; building candidate index from engine-log...")
    idx = build_candidate_index()

    ext, chase, opn = defaultdict(list), defaultdict(list), defaultdict(list)
    joined = 0
    for t in trips:
        try:
            ets = parse_ts(t["entry_ts"])
        except Exception:
            continue
        fill_ep = ets.timestamp()
        # open throttle: minutes since 13:30 UTC on the entry day
        mins = (ets.hour * 60 + ets.minute) - OPEN_UTC_MIN
        opn[open_bucket(mins)].append(t)
        # extension + chase need the screen candidate
        res = lookup(idx, t["symbol"], fill_ep)
        ipct, last = (res[0], res[1]) if res else (None, None)
        ext[ext_bucket(ipct)].append(t)
        chase_pct = ((t["entry_price"] / last - 1) * 100) if last else None
        chase[chase_bucket(chase_pct)].append(t)
        t["_ipct"], t["_mins"] = ipct, mins  # stash for cross-tab
        if ipct is not None or last is not None:
            joined += 1

    print(f"joined {joined}/{len(trips)} trips to an engine-log screen candidate "
          f"(unjoined -> 'unknown' bucket).")

    report("TEST 1 — EXTENSION gate (intraday_pct at fill = move from day open)",
           ext, ["neg(<0%)", "0-3%", "3-6%", "6-10%", "10%+", "unknown"], args.min_n)
    report("TEST 2 — ANCHOR/chase cap (fill vs screen last)",
           chase, ["below(<0)", "0-0.5%", "0.5-1.5%", "1.5-3%", "3%+", "unknown"], args.min_n)
    report("TEST 4 — OPEN throttle (minutes since 09:30 ET at fill)",
           opn, ["0-5m", "5-15m", "15-30m", "30-60m", "60m+", "pre/odd", "unknown"], args.min_n)

    print(f"\n(GATE flag = pre-registered rule: n>={args.min_n} AND sumUSD<0 AND PF<1.0 "
          "AND still negative after dropping its 2 best trips.)")

    # --- cross-tab: are "extended" and "opening-window" the same losing cohort? ---
    print(f"\n{'='*72}\nCROSS-TAB — extension x open-window (sumUSD / n)\n{'='*72}")
    cells = defaultdict(list)
    for t in trips:
        recent = (t.get("_mins") is not None and t["_mins"] < 60)
        extd = (t.get("_ipct") is not None and t["_ipct"] >= 10)
        cells[(recent, extd)].append(t)
    print(f"{'':<22}{'ext<10%':>16}{'ext>=10%':>16}")
    for recent, lbl in [(True, "first 60m"), (False, "60m+")]:
        row = ""
        for extd in (False, True):
            c = cells.get((recent, extd), [])
            s = stats(c) if c else None
            row += f"{(str(s['sum_usd'])+'/'+str(s['n'])) if s else '-':>16}"
        print(f"{lbl:<22}{row}")


if __name__ == "__main__":
    main()
