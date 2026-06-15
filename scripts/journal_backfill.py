#!/usr/bin/env python3
"""journal_backfill.py — rebuild the daily trade blotters from BROKER TRUTH + log narrative.

The per-day markdown blotters (data/journal/trades-<date>.md) were written incrementally by
trade_log.py off the event log, so on the live path ~half the SELL bullets read "@ ?  [placed —
fill unconfirmed]" with no realized P&L — the market-exit / external-close drift documented in
reconcile_ledger.py. This regenerates those days with GOOD data:

  • execution (price, qty, realized $, %) from broker-confirmed fills (get_equity_orders)
  • narrative (the DD thesis on entry, the manage/exit reasoning, conviction/book/PEAD) joined
    from data/trades.jsonl by order_id (fallback: symbol+side+minute)
  • a per-day header summary: # trades, realized for the day, win rate
  • paper fills on a live day are kept verbatim from the log (paper sells already carry a price)

Only days with LIVE broker activity are rewritten; pure-paper days (pre-2026-06-08) are left
untouched. Originals are backed up to data/journal/_pre_backfill/ before the first overwrite.

Usage:
    python3 scripts/journal_backfill.py            # rebuild live days
    python3 scripts/journal_backfill.py --dry-run  # print what it would write, touch nothing
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reconcile_ledger
from trade_ledger import _dedupe_order_lifecycle
from trade_log import classify_exit, EXIT_LABEL

REPO = Path(__file__).resolve().parent.parent
TRADES_LOG = REPO / "data" / "trades.jsonl"
JOURNAL = REPO / "data" / "journal"
BACKUP = JOURNAL / "_pre_backfill"
ET = ZoneInfo("America/New_York")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fmt_usd(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _et(dt_iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(dt_iso.replace("Z", "+00:00")).astimezone(ET)
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------- narrative join (from our log)
def narrative_index() -> tuple[dict, dict]:
    """Two lookups from trades.jsonl: by order_id (richest reason wins) and by (sym,side,utc-min)."""
    by_oid: dict[str, dict] = {}
    by_key: dict[tuple, dict] = {}
    if not TRADES_LOG.exists():
        return by_oid, by_key
    for line in TRADES_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        oid = r.get("order_id")
        if oid:
            prev = by_oid.get(str(oid))
            # keep the row with the longest reason — manage/DD prose beats a terse reconcile note
            if not prev or len(str(r.get("reason") or "")) > len(str(prev.get("reason") or "")):
                by_oid[str(oid)] = r
        key = (str(r.get("symbol", "")).upper(), str(r.get("side", "")).lower(), (r.get("ts_utc") or "")[:16])
        if key not in by_key or len(str(r.get("reason") or "")) > len(str(by_key[key].get("reason") or "")):
            by_key[key] = r
    return by_oid, by_key


# ---------------------------------------------------------------- bullet rendering
def buy_bullet(o: dict, n: dict) -> str:
    t = _et(o.get("last_transaction_at") or o.get("created_at") or "")
    ts = t.strftime("%H:%M:%S") if t else "??:??:??"
    qty = _f(o.get("cumulative_quantity")) or _f(o.get("quantity")) or 0
    px = _f(o.get("average_price")) or _f(o.get("price"))
    extra = []
    if n.get("conviction"):
        extra.append(str(n["conviction"]))
    if n.get("book"):
        extra.append(f"book:{n['book']}")
    if n.get("pead_qualified") is True:
        extra.append("PEAD✓")
    if n.get("stop_price") is not None:
        extra.append(f"stop {n['stop_price']}")
    if n.get("take_profit_price") is not None:
        extra.append(f"tp {n['take_profit_price']}")
    tag = f"  ({', '.join(extra)})" if extra else ""
    reason = " ".join(str(n.get("reason") or "").split())
    rtxt = f"  — {reason}" if reason else ""
    return f"- `{ts}` **BUY** {qty:g} {o.get('symbol')} @ {px:.4f}{tag}{rtxt}"


def sell_bullet(o: dict, n: dict, rz: dict | None) -> str:
    t = _et(o.get("last_transaction_at") or o.get("created_at") or "")
    ts = t.strftime("%H:%M:%S") if t else "??:??:??"
    qty = _f(o.get("cumulative_quantity")) or _f(o.get("quantity")) or 0
    px = _f(o.get("average_price")) or _f(o.get("price"))
    extra = []
    if rz:
        extra.append(fmt_usd(rz["realized"]))
        if rz.get("pct") is not None:
            extra.append(f"{rz['pct']:+.1f}%")
        extra.append(EXIT_LABEL.get(rz["exit_type"], rz["exit_type"]))
        if rz.get("hold"):
            extra.append(f"held {rz['hold']}")
    else:
        extra.append(EXIT_LABEL.get(classify_exit(n.get("reason", "")), "discretionary"))
    tag = f"  ({', '.join(extra)})"
    reason = " ".join(str(n.get("reason") or "").split())
    rtxt = f"  — {reason}" if reason else ""
    return f"- `{ts}` **SELL** {qty:g} {o.get('symbol')} @ {px:.4f}{tag}{rtxt}"


def paper_bullet(r: dict) -> str:
    """Verbatim-ish bullet for a paper fill kept from the log (paper already carries a price)."""
    t = (r.get("ts_et") or r.get("ts_utc") or "")[11:19] or "??:??:??"
    side = str(r.get("side", "")).upper()
    px = r.get("price")
    extra = []
    if side == "SELL":
        if r.get("realized_usd") is not None:
            extra.append(fmt_usd(float(r["realized_usd"])))
        extra.append(EXIT_LABEL.get(r.get("exit_type"), r.get("exit_type") or "discretionary"))
    else:
        if r.get("book"):
            extra.append(f"book:{r['book']}")
    tag = f"  ({', '.join(extra)})" if extra else ""
    reason = " ".join(str(r.get("reason") or "").split())
    rtxt = f"  — {reason}" if reason else ""
    return f"- `{t}` **{side}** {r.get('qty')} {r.get('symbol')} @ {px} [paper]{tag}{rtxt}"


HEADER = (
    "# Trade blotter — {day}\n\n"
    "Rebuilt by `journal_backfill.py` from **broker-confirmed fills** (`get_equity_orders`) joined to "
    "the DD/manage narrative in `data/trades.jsonl`. Prices and realized P&L are settlement truth; "
    "paper fills (if any) are kept from the log and tagged `[paper]`.\n\n"
    "**Day:** {n} trades ({b} buys / {s} sells) · realized **{realized}** · win rate {wr}\n\n"
)


def build() -> tuple[dict[str, list[tuple[str, str]]], dict[str, dict]]:
    """Return ({ET-day: [(sort_ts, bullet), ...]}, {ET-day: summary}) for live-activity days."""
    orders = reconcile_ledger.fetch_broker_orders("filled")
    trips, _ = reconcile_ledger.fifo_round_trips(orders)
    by_oid, by_key = narrative_index()

    # realized aggregated per exit order id (a broker sell can close several FIFO chunks)
    rz_by_oid: dict[str, dict] = {}
    for t in trips:
        oid = t.get("exit_order_id")
        if not oid:
            continue
        e = rz_by_oid.setdefault(oid, {"realized": 0.0, "entry_cost": 0.0, "qty": 0.0,
                                       "exit_px": t["exit_price"], "hold": t["hold"], "sym": t["symbol"]})
        e["realized"] += t["realized_usd"]
        e["entry_cost"] += t["entry_price"] * t["qty"]
        e["qty"] += t["qty"]
    for e in rz_by_oid.values():
        avg_entry = e["entry_cost"] / e["qty"] if e["qty"] else 0
        e["pct"] = (e["exit_px"] / avg_entry - 1) * 100 if avg_entry else None
        e["exit_type"] = "other"  # refined per-order from the matched reason in the loop below

    days: dict[str, list[tuple[str, str]]] = defaultdict(list)
    day_stat: dict[str, dict] = defaultdict(lambda: {"b": 0, "s": 0, "realized": 0.0, "w": 0, "n": 0})

    for o in orders:
        if o.get("state") != "filled":
            continue
        side = str(o.get("side", "")).lower()
        ts = o.get("last_transaction_at") or o.get("created_at") or ""
        et = _et(ts)
        if not et:
            continue
        day = et.strftime("%Y-%m-%d")
        n = by_oid.get(str(o.get("id"))) or by_key.get(
            (str(o.get("symbol", "")).upper(), side, ts[:16]), {})
        if side == "buy":
            days[day].append((ts, buy_bullet(o, n)))
            day_stat[day]["b"] += 1
        else:
            rz = rz_by_oid.get(o.get("id"))
            if rz:  # prefer the exit_type our log already booked (keeps stop/scale/TP consistent
                    # with reconcile_ledger), else classify the matched reason text
                rz["exit_type"] = (n.get("exit_type") or classify_exit(n.get("reason", ""))) if n else "other"
            days[day].append((ts, sell_bullet(o, n, rz)))
            day_stat[day]["s"] += 1
            if rz:
                day_stat[day]["realized"] += rz["realized"]
                day_stat[day]["n"] += 1
                day_stat[day]["w"] += 1 if rz["realized"] > 1e-9 else 0

    # fold in paper fills that fall on a live day (only 2026-06-08 in practice)
    live_days = set(days)
    if TRADES_LOG.exists():
        paper = [json.loads(l) for l in TRADES_LOG.read_text().splitlines() if l.strip()]
        paper = [r for r in _dedupe_order_lifecycle(paper)
                 if r.get("mode") == "paper" and r.get("status") in ("filled", "placed")]
        for r in paper:
            day = (r.get("ts_et") or r.get("ts_utc") or "")[:10]
            if day in live_days:
                days[day].append(((r.get("ts_utc") or ""), paper_bullet(r)))
                day_stat[day]["b" if r.get("side") == "buy" else "s"] += 1

    return days, day_stat


def render_day(day: str, items: list[tuple[str, str]], st: dict) -> str:
    wr = f"{round(100 * st['w'] / st['n'])}%" if st["n"] else "n/a"
    head = HEADER.format(day=day, n=st["b"] + st["s"], b=st["b"], s=st["s"],
                         realized=fmt_usd(st["realized"]), wr=wr)
    body = "\n".join(b for _, b in sorted(items, key=lambda x: x[0]))
    return head + body + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        days, stat = build()
    except reconcile_ledger.rh_direct.DirectError as e:
        print(f"broker fetch failed: {e}", file=sys.stderr)
        return 2

    if not args.dry_run:
        BACKUP.mkdir(parents=True, exist_ok=True)
    for day in sorted(days):
        path = JOURNAL / f"trades-{day}.md"
        out = render_day(day, days[day], stat[day])
        if args.dry_run:
            print(f"\n===== would write {path} ({len(out)} bytes) =====")
            print(out[:600])
            continue
        if path.exists() and not (BACKUP / path.name).exists():
            shutil.copy2(path, BACKUP / path.name)  # one-time backup of the original
        path.write_text(out)
        print(f"wrote {path}  ({stat[day]['b']+stat[day]['s']} trades, "
              f"realized {fmt_usd(stat[day]['realized'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
