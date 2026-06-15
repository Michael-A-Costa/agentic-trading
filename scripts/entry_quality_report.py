#!/usr/bin/env python3
"""entry_quality_report.py — Phase 0 of the entry-gate plan (entry-gate-plan-2026-06-15.md).

The exit thread proved discretionary EXIT timing is a near-wash (exit_counterfactual.py: −0.31%/trade;
held-to-now: ~+$4 same-day) — yet discretionary exits booked −$61 realized. That can only mean the loss
was baked in at ENTRY: the sell just closed a position that was a loser from the buy. This tool locates
that loss by entry ATTRIBUTE so the fix lands on the right control point (screen vs DD vs sizing) instead
of a blind dial.

How: take broker-truth round-trips (data/ledger_truth.json, the settlement record from
reconcile_ledger.py --write) and JOIN each back to its trades.jsonl BUY row (by symbol + nearest entry
time — the same matching the §A19 null-price backfill uses), attaching the logged entry attributes
(conviction, thesis_type, pead_qualified, book, iv30, ...). Then split realized P&L by each attribute.

Pre-registered decision rule (frozen in the plan, do NOT loosen after looking): a bucket is a gate
candidate only if, over >=MIN_N closed round-trips, it is realized-$ negative AND profit-factor < 1.0 AND
the loss survives dropping its 2 best trades (not an outlier artifact). This tool flags those buckets; it
changes no dials.

Usage:
  python3 scripts/reconcile_ledger.py --write          # refresh broker truth first
  python3 scripts/entry_quality_report.py              # all live round-trips, every attribute split
  python3 scripts/entry_quality_report.py --min-n 15   # relax the pre-registered n bar (reports only)
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "data" / "trades.jsonl"
LEDGER_TRUTH = REPO / "data" / "ledger_truth.json"
VOL_BACKFILL = REPO / "data" / "entry_vol_backfill.json"

# Attributes graded, in the order printed. Each: (key, label, bucketer(value) -> str | None).
# bucketer returns the display bucket, or None to drop the trip from that split (missing data).
IV_EDGES = [(0, "iv<60"), (60, "iv60-90"), (90, "iv90-120"), (120, "iv120+")]


def _iv_bucket(v):
    if v is None:
        return None
    for lo, lbl in reversed(IV_EDGES):
        if v >= lo:
            return lbl
    return IV_EDGES[0][1]


def _truthy(v):
    return "yes" if v else "no"


ATTRS = [
    ("conviction", "conviction", lambda v: str(v) if v else "unset"),
    ("thesis_type", "thesis_type", lambda v: str(v) if v else "unset"),
    ("book", "book", lambda v: str(v) if v else "untagged"),
    ("pead_qualified", "pead_qualified", _truthy),
    ("washout_reversal", "washout_reversal", _truthy),
    ("hold_intent", "hold_intent", lambda v: str(v) if v else "unset"),
    ("iv30", "iv30 band", _iv_bucket),
]


def _parse_dt(s: str):
    """Parse either '2026-06-15 14:07:13' (broker truth, UTC-naive) or ISO w/ tz (buy row)."""
    s = str(s).strip().replace("Z", "+00:00")
    if " " in s and "T" not in s:           # broker-truth "date time" -> ISO
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def load_buy_attrs() -> dict[str, list[tuple[datetime, dict]]]:
    """Per-symbol time-sorted [(entry_dt_utc, attrs), ...] from live BUY rows in trades.jsonl,
    enriched with the entry-vol sidecar (iv30/rvol20) the same way exit_counterfactual does."""
    try:
        vol = json.loads(VOL_BACKFILL.read_text())
    except (OSError, ValueError):
        vol = {}
    out: dict[str, list[tuple[datetime, dict]]] = defaultdict(list)
    if not LEDGER.exists():
        return out
    for line in LEDGER.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if str(r.get("side", "")).lower() != "buy" or r.get("mode") not in ("live", "live-dryrun"):
            continue
        dt = _parse_dt(r.get("ts_utc") or "")
        if dt is None:
            continue
        sym = str(r.get("symbol", "")).upper()
        date = (r.get("ts_et") or r.get("ts_utc") or "")[:10]
        bf = vol.get(f"{sym}:{date}", {})
        attrs = {k: r.get(k) for k in ("conviction", "thesis_type", "book", "pead_qualified",
                                       "washout_reversal", "hold_intent", "iv30", "rvol20")}
        if attrs.get("iv30") is None:
            attrs["iv30"] = bf.get("iv30")
        if attrs.get("rvol20") is None:
            attrs["rvol20"] = bf.get("rvol20")
        out[sym].append((dt, attrs))
    for v in out.values():
        v.sort(key=lambda x: x[0])
    return out


def match_entry(sym: str, entry_ts: str, buys: dict, window_min: float = 10.0):
    """The buy row for sym closest to the broker entry fill time, within +/- window_min. None if no
    match (entry older than our log, or a non-agentic fill)."""
    t = _parse_dt(entry_ts)
    cands = buys.get(sym, [])
    if t is None or not cands:
        return None
    best, gap = None, window_min * 60
    for dt, attrs in cands:
        g = abs((dt - t).total_seconds())
        if g <= gap:
            best, gap = attrs, g
    return best


def stats(rows: list[dict]) -> dict:
    """Realized-P&L stats for one bucket of round-trips (each carries realized_usd)."""
    rs = [r["realized_usd"] for r in rows]
    wins = [x for x in rs if x > 1e-9]
    losses = [x for x in rs if x < -1e-9]
    gw, gl = sum(wins), -sum(losses)
    # drop-top-2 robustness: realized with the 2 best trades removed (per the pre-registered rule)
    drop2 = sum(sorted(rs)[:-2]) if len(rs) > 2 else sum(rs)
    return {"n": len(rs), "realized": sum(rs), "win_pct": 100 * len(wins) / len(rs) if rs else 0,
            "avg_win": gw / len(wins) if wins else 0.0, "avg_loss": -gl / len(losses) if losses else 0.0,
            "pf": (gw / gl) if gl > 1e-9 else None, "drop2": drop2}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-n", type=int, default=20, help="pre-registered round-trip bar for a gate candidate")
    ap.add_argument("--window-min", type=float, default=10.0, help="entry-time match window")
    args = ap.parse_args()

    if not LEDGER_TRUTH.exists():
        print(f"{LEDGER_TRUTH.name} missing — run: python3 scripts/reconcile_ledger.py --write")
        return 2
    d = json.loads(LEDGER_TRUTH.read_text())
    trips = d.get("round_trips", [])
    buys = load_buy_attrs()

    matched, unmatched = [], 0
    for t in trips:
        a = match_entry(t["symbol"], t.get("entry_ts", ""), buys, args.window_min)
        if a is None:
            unmatched += 1
            continue
        matched.append(dict(t, _attrs=a))

    tot = sum(t["realized_usd"] for t in matched)
    print(f"ENTRY-QUALITY REPORT (Phase 0) — {len(matched)}/{len(trips)} broker round-trips joined to a "
          f"live buy row ({unmatched} unmatched: pre-log or non-agentic)")
    print(f"matched realized P&L: ${tot:+.2f}   (pre-registered gate bar: n>={args.min_n}, "
          f"realized<0, PF<1.0, survives drop-top-2)\n")

    for key, label, bucketer in ATTRS:
        groups: dict[str, list[dict]] = defaultdict(list)
        for t in matched:
            b = bucketer(t["_attrs"].get(key))
            if b is not None:
                groups[b].append(t)
        if not groups:
            continue
        rows = sorted(((b, stats(g)) for b, g in groups.items()), key=lambda x: x[1]["realized"])
        print(f"── by {label} " + "─" * (62 - len(label)))
        print(f"   {'bucket':<14}{'n':>4}{'realized':>11}{'win%':>6}{'avgW':>8}{'avgL':>8}{'PF':>6}"
              f"{'drop2':>9}  flag")
        for b, s in rows:
            pf = f"{s['pf']:.2f}" if s["pf"] is not None else "  ∞"
            # A gate target must be an ACTIONABLE attribute value. 'unset'/'untagged' is missing metadata —
            # in practice the pre-tagging cohort (early live days), which you can't gate on. Never a candidate.
            actionable = b not in ("unset", "untagged")
            cand = (actionable and s["n"] >= args.min_n and s["realized"] < 0
                    and (s["pf"] is not None and s["pf"] < 1.0) and s["drop2"] < 0)
            thin = actionable and s["n"] < args.min_n and s["realized"] < 0 and (s["pf"] is None or s["pf"] < 1.0)
            flag = ("◀ GATE CANDIDATE" if cand else ("(thin: watch)" if thin
                    else ("(uncategorized — not gate-able)" if not actionable and s["realized"] < 0 else "")))
            print(f"   {b:<14}{s['n']:>4}{s['realized']:>+10.2f}{s['win_pct']:>5.0f}%{s['avg_win']:>+8.2f}"
                  f"{s['avg_loss']:>+8.2f}{pf:>6}{s['drop2']:>+9.2f}  {flag}")
        print()

    print("Reading guide: a GATE CANDIDATE bucket clears the frozen rule and is a defensible place to "
          "tighten the entry gate (Phase 2). 'thin: watch' = negative but under the n bar — keep logging, "
          "do not act. Everything else is fine or positive. No dials changed by this report.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
