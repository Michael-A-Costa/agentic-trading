#!/usr/bin/env python3
"""open_select_backtest.py — pick the open-window selector config from broker-truth round-trips.

The open-window controls (decide.py + OPEN_GATE_*/OPEN_SELECT_*) have three dials:
  * gate_ext   — drop in-window entries whose intraday_pct (move from day open) >= this
  * floor      — drop in-window commits below this conviction (pead-qualified exempt)
  * cap        — keep at most this many in-window entries PER DAY (the best by score)

This replays each config against the 97 broker-truth round-trips (data/ledger_truth.json), restricted
to OPEN-WINDOW entries, grouped by ET day, and reports what each config would have KEPT vs DROPPED in
realized $. We trust realized P&L (exit timing is a near-wash, exit_counterfactual.py), so a good config
is one whose DROPPED set is net-negative (shedding losers) and whose KEPT set holds/raises total $ and PF.

Joins (same as entry_timing_replay.py): round-trip -> trades.jsonl buy (symbol + nearest entry ts, for
conviction + pead_qualified) and -> engine-log candidate at-or-before the fill (for intraday_pct + range_pos).
Reports only; changes no dials.
"""
from __future__ import annotations
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
TRUTH = REPO / "data" / "ledger_truth.json"
TRADES = REPO / "data" / "trades.jsonl"
ENGINE = REPO / "data" / "engine-log.jsonl"
ET = ZoneInfo("America/New_York")
RANK = {"high": 3, "medium": 2, "low": 1}


def parse_utc(s: str) -> datetime:
    s = s.strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.fromisoformat(s).astimezone(timezone.utc)


def trades_index():
    """symbol -> sorted [(epoch_utc, conviction, pead_qualified)] for every BUY fill."""
    idx = defaultdict(list)
    if not TRADES.exists():
        return idx
    for line in TRADES.open():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("side") != "buy" or not r.get("ts_utc"):
            continue
        idx[r["symbol"].upper()].append((parse_utc(r["ts_utc"]).timestamp(),
                                         (r.get("conviction") or "low"),
                                         r.get("pead_qualified")))
    for k in idx:
        idx[k].sort()
    return idx


def candidate_index():
    """symbol -> sorted [(epoch, intraday_pct, range_pos)] from every decide screen candidate."""
    idx = defaultdict(list)
    for line in ENGINE.open():
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
            idx[c["symbol"].upper()].append((ep, c.get("intraday_pct"), c.get("range_pos")))
    for k in idx:
        idx[k].sort()
    return idx


def nearest_before(seq, ep, tol=None):
    """Last entry whose epoch <= ep (+ small forward tolerance); seq is sorted by epoch."""
    best = None
    for item in seq:
        if item[0] <= ep + (tol or 0):
            best = item
        else:
            break
    return best


def build_open_entries(window_min):
    """All open-window round-trips with joined (day, mins, conviction, pead, ext, range_pos, $)."""
    truth = json.load(TRUTH.open())["round_trips"]
    tidx, cidx = trades_index(), candidate_index()
    out = []
    for t in truth:
        ets = parse_utc(t["entry_ts"])
        et_local = ets.astimezone(ET)
        mins = (et_local.hour * 60 + et_local.minute) - (9 * 60 + 30)
        if not (0 <= mins < window_min):
            continue
        ep = ets.timestamp()
        tr = nearest_before(tidx.get(t["symbol"].upper(), []), ep, tol=120)   # buy fill within 2 min
        cd = nearest_before(cidx.get(t["symbol"].upper(), []), ep, tol=30)
        out.append({
            "symbol": t["symbol"].upper(), "day": et_local.strftime("%Y-%m-%d"), "mins": mins,
            "usd": t["realized_usd"],
            "conv": (tr[1] if tr else "low"),
            "pead": (tr[2] if tr else None),
            "ext": (cd[1] if cd else None),
            "rp": (cd[2] if cd else None),
        })
    return out


def score(e):
    s = RANK.get((e["conv"] or "low").lower(), 1) * 1000
    if e["pead"] is True:
        s += 500
    ext = e["ext"]
    if ext is not None:
        if 3.0 <= ext < 10.0:
            s += 200
        elif 0.0 <= ext < 3.0:
            s -= 100
        elif ext < 0.0:
            s -= 50
    s += int((e["rp"] or 0.0) * 10)
    return s


def simulate(entries, gate_ext, floor_name, cap):
    """Return (kept_list, dropped_list) after gate -> floor -> per-day score-rank -> cap."""
    floor = RANK.get((floor_name or "low").lower(), 1)
    by_day = defaultdict(list)
    dropped = []
    for e in entries:
        if gate_ext is not None and e["ext"] is not None and e["ext"] >= gate_ext:
            dropped.append(e); continue                              # extension gate
        if RANK.get((e["conv"] or "low").lower(), 1) < floor and e["pead"] is not True:
            dropped.append(e); continue                              # conviction floor (pead exempt)
        by_day[e["day"]].append(e)
    kept = []
    for es in by_day.values():
        es.sort(key=score, reverse=True)
        kept.extend(es[:cap])
        dropped.extend(es[cap:])
    return kept, dropped


def stat(lst):
    n = len(lst); tot = sum(e["usd"] for e in lst)
    wins = sum(e["usd"] for e in lst if e["usd"] > 0)
    loss = -sum(e["usd"] for e in lst if e["usd"] < 0)
    pf = (wins / loss) if loss > 0 else (float("inf") if wins > 0 else 0.0)
    wr = sum(1 for e in lst if e["usd"] > 0) / n if n else 0.0
    return n, tot, pf, wr


def main():
    WIN = 60
    entries = build_open_entries(WIN)
    bn, btot, bpf, bwr = stat(entries)
    print(f"open window = first {WIN} min | {bn} broker-truth entries in-window")
    print(f"BASELINE (take all): n={bn}  $={btot:+.2f}  PF={bpf:.2f}  win%={bwr*100:.0f}\n")
    # feature coverage
    miss_ext = sum(1 for e in entries if e["ext"] is None)
    print(f"(join coverage: ext missing for {miss_ext}/{bn}, conviction defaults to 'low' when no trade match)\n")

    print(f"{'gate':>5} {'floor':>7} {'cap':>4} | {'keptN':>5} {'kept$':>8} {'keptPF':>6} {'kpWin%':>6} "
          f"| {'dropN':>5} {'drop$':>8}  verdict")
    print("-" * 92)
    best = None
    for gate_ext in (None, 12, 10, 8, 6):
        for floor_name in ("low", "medium", "high"):
            for cap in (1, 2, 3, 4, 6, 999):
                kept, dropped = simulate(entries, gate_ext, floor_name, cap)
                kn, ktot, kpf, kwr = stat(kept)
                dn, dtot, _, _ = stat(dropped)
                # objective: keep the most $ while the dropped set is non-positive (shedding losers).
                ok = dtot <= 0
                obj = ktot + (0 if ok else -abs(dtot))   # penalize dropping winners
                row = (f"{str(gate_ext):>5} {floor_name:>7} {cap if cap<999 else 'inf':>4} | "
                       f"{kn:>5} {ktot:>+8.2f} {kpf:>6.2f} {kwr*100:>5.0f}% | "
                       f"{dn:>5} {dtot:>+8.2f}  {'shed-losers' if ok else 'drops-winners'}")
                print(row)
                if best is None or obj > best[0]:
                    best = (obj, gate_ext, floor_name, cap, kn, ktot, kpf, dn, dtot)
            print()
    bo = best
    print("=" * 92)
    print(f"BEST by (max kept-$ with dropped set <=0): gate>={bo[1]} floor={bo[2]} cap={bo[3]}")
    print(f"   -> keep {bo[4]} entries for ${bo[5]:+.2f} (PF {bo[6]:.2f}); drop {bo[7]} for ${bo[8]:+.2f}")
    print(f"   vs baseline keep {bn} for ${btot:+.2f} (PF {bpf:.2f})")


if __name__ == "__main__":
    main()
