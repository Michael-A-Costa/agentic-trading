#!/usr/bin/env python3
"""trade_ledger.py — read the unified trade history (data/trades.jsonl) two ways.

Where pnl_report.py works off the fat per-tick engine log, this reads the dedicated trade ledger
that apply_decision.py (paper) and live_execute.py (live) write via trade_log.py — one row per
executed fill. It surfaces what nothing else does:

  1. a chronological BLOTTER — every fill, in order, one line each (optionally filtered)
  2. reconstructed ROUND-TRIPS — entries paired to exits (FIFO) per symbol, with hold time,
     entry/exit avg price, realized $ and %, and the exit type that closed each chunk

Usage:
    python3 scripts/trade_ledger.py                  # blotter + round-trips, whole ledger
    python3 scripts/trade_ledger.py --since 2026-06-04
    python3 scripts/trade_ledger.py --symbol NVDA
    python3 scripts/trade_ledger.py --round-trips    # round-trips only (skip the blotter)
    python3 scripts/trade_ledger.py --blotter        # blotter only
    python3 scripts/trade_ledger.py --mode paper     # filter by mode (paper/live/live-dryrun)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trade_log import EXIT_LABEL  # shared exit-type labels

REPO = Path(__file__).resolve().parent.parent
DEFAULT_LEDGER = REPO / "data" / "trades.jsonl"


def fmt_usd(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def load_rows(path: Path, since: str | None, symbol: str | None, mode: str | None,
              book: str | None = None) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"no trade ledger at {path} — no executed trades recorded yet.")
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue  # tolerate a torn line rather than abort
        if since and (r.get("ts_et", "") or "")[:10] < since:
            continue
        if symbol and str(r.get("symbol", "")).upper() != symbol.upper():
            continue
        if mode and mode != "all" and str(r.get("mode", "")) != mode:
            continue
        if book and str(r.get("book") or "untagged") != book:
            continue   # two-book filter: pead / disco / untagged (pre-split rows)
        rows.append(r)
    return _dedupe_order_lifecycle(rows)


def _dedupe_order_lifecycle(rows: list[dict]) -> list[dict]:
    """Collapse one order's lifecycle rows into its terminal truth (P6: placed != filled).

    A live order can appear up to twice: status=placed at the placing tick, then a reconcile row
    with status=filled (real fill price) or status=dead (never filled). Per order_id:
      - any 'dead' row  -> the order never executed; drop EVERY row for that id
      - a 'filled' row  -> keep it (real price), drop the superseded 'placed' row
      - 'placed' only   -> keep it (legacy rows / fill-not-yet-confirmed)
    Rows without an order_id (paper fills, external closures) pass through untouched."""
    by_oid: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        oid = r.get("order_id")
        if oid:
            by_oid[str(oid)].append(r)
    drop: set[int] = set()
    for oid, group in by_oid.items():
        if len(group) < 2 and group[0].get("status") != "dead":
            continue
        statuses = {g.get("status") for g in group}
        if "dead" in statuses:
            drop.update(id(g) for g in group)                       # never executed
        elif "filled" in statuses:
            drop.update(id(g) for g in group if g.get("status") == "placed")  # superseded
    return [r for r in rows if id(r) not in drop]


def show_blotter(rows: list[dict]) -> None:
    print(f"\n{'=' * 78}\nBLOTTER — {len(rows)} executed trade(s)\n{'=' * 78}")
    if not rows:
        print("none in window.")
        return
    print(f"{'date/time (ET)':<20}{'mode':<12}{'side':<5}{'qty':>10} {'symbol':<7}"
          f"{'price':>10}{'realized':>12}  exit/notes")
    print("-" * 78)
    for r in rows:
        ts = (r.get("ts_et") or r.get("ts_utc") or "")[:19].replace("T", " ")
        side = str(r.get("side", "")).upper()
        rz = r.get("realized_usd")
        rz_s = fmt_usd(float(rz)) if rz is not None else ""
        note = EXIT_LABEL.get(r.get("exit_type"), r.get("exit_type") or "") if side == "SELL" \
            else (r.get("stop_type") or "")
        print(f"{ts:<20}{str(r.get('mode','')):<12}{side:<5}{str(r.get('qty','')):>10} "
              f"{str(r.get('symbol','')):<7}{str(r.get('price','')):>10}{rz_s:>12}  {note}")


def _parse_ts(r: dict) -> datetime | None:
    raw = r.get("ts_utc") or r.get("ts_et")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _fmt_hold(buy_ts: datetime | None, sell_ts: datetime | None) -> str:
    if not buy_ts or not sell_ts:
        return "?"
    mins = (sell_ts - buy_ts).total_seconds() / 60.0
    if mins < 0:
        return "?"
    if mins < 60:
        return f"{mins:.0f}m"
    if mins < 60 * 24:
        return f"{mins/60:.1f}h"
    return f"{mins/1440:.1f}d"


def build_round_trips(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Pair exits to entries FIFO, per symbol. Each closed chunk is one round-trip.

    A scale-out or partial sell closes part of the oldest open lot; the remainder stays open.
    Buys with no matching sell yet are reported separately as still-open exposure.
    """
    open_lots: dict[str, deque] = defaultdict(deque)  # symbol -> deque of [qty, price, ts]
    trips: list[dict] = []
    for r in rows:
        sym = str(r.get("symbol", "")).upper()
        side = str(r.get("side", "")).lower()
        qty = float(r.get("qty") or 0.0)
        price = r.get("price")
        ts = _parse_ts(r)
        if qty <= 0 or price is None:
            continue
        if side == "buy":
            open_lots[sym].append([qty, float(price), ts])
            continue
        # sell: consume oldest open lots FIFO
        remaining = qty
        lots = open_lots[sym]
        while remaining > 1e-9 and lots:
            lot = lots[0]
            take = min(remaining, lot[0])
            realized = (float(price) - lot[1]) * take
            pnl_pct = (float(price) / lot[1] - 1.0) * 100.0 if lot[1] else 0.0
            trips.append({
                "symbol": sym, "qty": round(take, 6),
                "entry_price": lot[1], "exit_price": float(price),
                "realized_usd": round(realized, 2), "pnl_pct": round(pnl_pct, 2),
                "hold": _fmt_hold(lot[2], ts),
                "exit_type": r.get("exit_type", "other"),
                "exit_ts": (r.get("ts_et") or r.get("ts_utc") or "")[:19].replace("T", " "),
                "mode": r.get("mode"),
            })
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-9:
                lots.popleft()
        # a sell with no matching open lot (e.g. ledger starts mid-position) is ignored for pairing
    # leftover open lots
    open_positions = []
    for sym, lots in open_lots.items():
        for q, p, ts in lots:
            open_positions.append({"symbol": sym, "qty": round(q, 6), "entry_price": p,
                                   "entry_ts": ts.isoformat()[:19].replace("T", " ") if ts else "?"})
    return trips, open_positions


def show_round_trips(rows: list[dict]) -> None:
    trips, open_pos = build_round_trips(rows)
    print(f"\n{'=' * 78}\nROUND-TRIPS — {len(trips)} closed chunk(s)\n{'=' * 78}")
    if trips:
        print(f"{'exit time (ET)':<20}{'symbol':<7}{'qty':>9}{'entry':>9}{'exit':>9}"
              f"{'P&L':>11}{'P&L%':>8}{'hold':>7}  exit")
        print("-" * 78)
        total = 0.0
        for t in trips:
            total += t["realized_usd"]
            print(f"{t['exit_ts']:<20}{t['symbol']:<7}{t['qty']:>9}{t['entry_price']:>9}"
                  f"{t['exit_price']:>9}{fmt_usd(t['realized_usd']):>11}{t['pnl_pct']:>7.1f}%"
                  f"{t['hold']:>7}  {EXIT_LABEL.get(t['exit_type'], t['exit_type'])}")
        wins = [t for t in trips if t["realized_usd"] > 1e-9]
        print("-" * 78)
        print(f"net realized {fmt_usd(total)}   over {len(trips)} chunk(s)   "
              f"win-rate {100*len(wins)/len(trips):.0f}%")
    else:
        print("no closed round-trips in window.")

    if open_pos:
        print(f"\nstill open ({len(open_pos)} lot(s)):")
        for p in open_pos:
            print(f"  {p['symbol']:<7} qty {p['qty']}  entry {p['entry_price']}  since {p['entry_ts']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Blotter + round-trips from the unified trade ledger.")
    ap.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    ap.add_argument("--since", help="ET date floor, YYYY-MM-DD (inclusive)")
    ap.add_argument("--symbol", help="filter to one symbol")
    ap.add_argument("--mode", help="filter by mode: paper / live / live-dryrun / all. Default: "
                    "$TRADING_MODE when set (so a live shell reads live truth), else all")
    ap.add_argument("--blotter", action="store_true", help="show only the chronological blotter")
    ap.add_argument("--round-trips", action="store_true", help="show only the round-trips")
    ap.add_argument("--book", help="filter by virtual book: pead / disco / untagged "
                    "(two-book split, strategies/two-book-v2-plan.md)")
    args = ap.parse_args()

    # Mixed paper+live stats masquerading as truth is how the win-rate question went unanswerable
    # (remediation plan P4) — default to the ambient TRADING_MODE and always SAY what's included.
    mode = args.mode or os.environ.get("TRADING_MODE") or "all"
    print(f"MODE: {mode}" + ("  (paper + live MIXED — pass --mode live for live truth)"
                             if mode == "all" else ""))
    if args.book:
        print(f"BOOK: {args.book}")
    rows = load_rows(args.ledger, args.since, args.symbol, mode, args.book)
    if not rows:
        print("no trades in window.")
        return 0

    show_both = not (args.blotter or args.round_trips)
    if args.blotter or show_both:
        show_blotter(rows)
    if args.round_trips or show_both:
        show_round_trips(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
