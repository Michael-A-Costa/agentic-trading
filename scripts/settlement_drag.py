#!/usr/bin/env python3
"""Capital-velocity / settlement-drag report for the CASH account.

On a cash account every SELL sterilizes its proceeds for T+1 — they sit in `cash` but are NOT
in `buying_power` until they settle the next business day. The CASH_SETTLEMENT_GUARD already
stops us from spending them (no GFV), but the *strategy* cost is dead capital: a third of the
book can be stranded waiting to settle while qualified entries get deferred for "no settled cash".

This script quantifies that drag from the live engine log so a strategy change (cut turnover?
settlement-aware exits?) is decided on measured numbers, not vibes. It answers three questions:

  [A] How much capital sat idle?    unsettled / equity per ET day (avg + intraday peak).
  [B] How starved were we?          ticks where settled buying-power was near zero.
  [C] What did it actually cost?     DISTINCT qualified entries deferred for "no settled cash"
                                     that were never funded later the same day (the real misses;
                                     raw deferral COUNT overstates it — the same name is re-deferred
                                     every tick until cash frees up).

Caveat the numbers carry their own: the log's `unsettled` is our internal ledger ESTIMATE
(sell-ref price, pruned by date), which has run BELOW the broker truth (cash - buying_power);
real drag is if anything a bit worse. And a "missed" entry only *cost* us if it had edge —
this script counts misses, it does not price their counterfactual P&L (see --misses to list them
for a forward-return study).

Usage:
    python3 scripts/settlement_drag.py                  # whole live history, by ET day
    python3 scripts/settlement_drag.py --since 2026-06-10
    python3 scripts/settlement_drag.py --misses         # also list every never-funded deferred name
    python3 scripts/settlement_drag.py --include-dryrun # count live-dryrun ticks too
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_LOG = REPO / "data" / "engine-log.jsonl"


def load_live(log_path: Path, since: str | None, include_dryrun: bool) -> list[dict]:
    rows = []
    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            mode = r.get("mode", "")
            if mode == "live" or (include_dryrun and mode.startswith("live")):
                if since and (r.get("ts_et") or "") [:10] < since:
                    continue
                rows.append(r)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--since", help="ET date YYYY-MM-DD; only ticks on/after")
    ap.add_argument("--include-dryrun", action="store_true", help="include live-dryrun ticks (default: armed live only)")
    ap.add_argument("--misses", action="store_true", help="list every never-funded deferred symbol per day")
    args = ap.parse_args()

    live = load_live(Path(args.log), args.since, args.include_dryrun)
    if not live:
        print("no matching live ticks in", args.log)
        return

    span = f"{(live[0].get('ts_et') or '')[:10]} -> {(live[-1].get('ts_et') or '')[:10]}"
    print(f"settlement-drag report | {len(live)} live ticks | {span}\n")

    # [B] starvation threshold: a tick is "starved" when < $50 settled was deployable.
    STARVE_USD = 50.0

    byday: dict[str, list] = defaultdict(list)
    deferred: dict[str, set] = defaultdict(set)   # day -> {sym} deferred for no settled cash
    funded: dict[str, set] = defaultdict(set)     # day -> {sym} placed/filled (same day)
    starved: dict[str, int] = defaultdict(int)
    nticks: dict[str, int] = defaultdict(int)

    for r in live:
        day = (r.get("ts_et") or "")[:10]
        nticks[day] += 1
        eq = r.get("equity") or 0.0
        uns = r.get("unsettled") or 0.0
        sbp = r.get("settled_buying_power")
        if sbp is None:
            sbp = r.get("buying_power") or 0.0
        if eq:
            byday[day].append((uns / eq, uns, sbp, eq))
        if sbp < STARVE_USD:
            starved[day] += 1
        for res in (r.get("results") or []):
            sym = res.get("symbol", "?")
            if "no settled cash" in (res.get("reject_reason") or ""):
                deferred[day].add(sym)
            if res.get("status") in ("placed", "filled"):
                funded[day].add(sym)

    # [A]/[B] table
    print("[A] dead capital (unsettled/equity) + [B] capital starvation, per ET day")
    print(f"   {'day':<12}{'avg idle%':>10}{'peak idle%':>11}{'avg unsettled':>15}"
          f"{'min settled':>13}{'starved ticks':>15}")
    g_idle = g_uns = 0.0
    for d in sorted(byday):
        v = byday[d]
        aidle = sum(x[0] for x in v) / len(v) * 100
        pidle = max(x[0] for x in v) * 100
        auns = sum(x[1] for x in v) / len(v)
        minsbp = min(x[2] for x in v)
        g_idle += aidle
        g_uns += auns
        print(f"   {d:<12}{aidle:>9.1f}%{pidle:>10.1f}%{auns:>15.0f}"
              f"{minsbp:>13.0f}{starved[d]:>9}/{nticks[d]:<5}")
    ndays = len(byday)
    print(f"   {'MEAN':<12}{g_idle/ndays:>9.1f}%{'':>11}{g_uns/ndays:>15.0f}")

    # [C] real opportunity cost
    print("\n[C] qualified entries DEFERRED for no settled cash (distinct symbol x day)")
    tot_def = tot_miss = 0
    miss_freq: dict[str, int] = defaultdict(int)
    for d in sorted(deferred):
        defd = deferred[d]
        missed = defd - funded[d]   # deferred and never funded same day = real miss
        tot_def += len(defd)
        tot_miss += len(missed)
        for s in missed:
            miss_freq[s] += 1
        line = f"   {d:<12} deferred {len(defd):>3}   never-funded {len(missed):>3}"
        if args.misses and missed:
            line += "   " + " ".join(sorted(missed))
        print(line)
    print(f"\n   TOTAL distinct deferred: {tot_def}   never funded same day (real misses): {tot_miss}")
    if miss_freq:
        top = sorted(miss_freq.items(), key=lambda x: -x[1])[:10]
        print("   most-repeated misses:", ", ".join(f"{s}x{n}" if n > 1 else s for s, n in top))
    print("\n   NOTE: a miss only COST us if it had edge — this counts misses, it does not price")
    print("   their forward P&L. Our own research finds the entry signal weak intraday, so the")
    print("   drag may be less harmful than it looks. Use --misses to seed a forward-return study.")


if __name__ == "__main__":
    main()
