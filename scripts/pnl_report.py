#!/usr/bin/env python3
"""Realized-P&L + exit-type breakdown from the engine log.

Reads data/engine-log.jsonl (append-only decision/fill record written by apply_decision.py)
and summarizes what actually happened: realized P&L, win rate, and — the reason this exists —
*how* positions were exited (stop / take-profit / EOD flatten / time-stop / scale-out trim), so
the scale-out ladder's effect is visible session-over-session rather than inferred.

Each filled SELL carries a cost-basis `realized_usd` (price - entry_price) * qty, so realized
P&L is summed directly off the fills — no quote lookup needed. Exit *type* is parsed from the
sell `reason` string the deterministic screen wrote (see tick_context.py).

Usage:
    python3 scripts/pnl_report.py                 # whole log
    python3 scripts/pnl_report.py --since 2026-06-04
    python3 scripts/pnl_report.py --by-day        # one block per ET trading day
    python3 scripts/pnl_report.py --log data/engine-log.jsonl --state data/paper_state.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trade_log import classify_exit  # single source of truth for exit-type classification

REPO = Path(__file__).resolve().parent.parent
DEFAULT_LOG = REPO / "data" / "engine-log.jsonl"
DEFAULT_STATE = REPO / "data" / "paper_state.json"

# Display order + labels for the breakdown table (keys match trade_log.classify_exit()).
EXIT_ORDER = ["take_profit", "scale_out", "winddown", "stop", "eod_flatten", "time_stop", "test", "other"]
EXIT_LABEL = {
    "take_profit": "take-profit (full)", "scale_out": "scale-out (partial)",
    "winddown": "EOD wind-down (green)",
    "stop": "stop-loss", "eod_flatten": "EOD flatten", "time_stop": "time-stop (stalled)",
    "test": "unit-test", "other": "other/discretionary",
}


def load_records(log_path: Path, since: str | None, mode: str | None = None) -> list[dict]:
    if not log_path.exists():
        raise SystemExit(f"no engine log at {log_path}")
    recs = []
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue  # skip a torn line rather than abort the whole report
        if since and (r.get("ts_et", "")[:10] < since):
            continue
        if mode and mode != "all" and str(r.get("mode", "")) != mode:
            continue
        recs.append(r)
    return recs


def iter_fills(recs: list[dict]):
    """Yield (et_date, result) for every FILLED result across all records."""
    for r in recs:
        et_date = r.get("ts_et", "")[:10]
        for res in r.get("results", []):
            if res.get("status") == "filled":
                yield et_date, res


def fmt_usd(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def summarize(recs: list[dict], title: str, book: str | None = None) -> None:
    sells = [res for _, res in iter_fills(recs) if res.get("side") == "sell"]
    buys = [res for _, res in iter_fills(recs) if res.get("side") == "buy"]
    if book is not None:   # two-book split: 'pead' / 'disco' / 'untagged' (pre-split rows)
        sells = [s for s in sells if str(s.get("book") or "untagged") == book]
        buys = [b for b in buys if str(b.get("book") or "untagged") == book]

    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")
    print(f"records: {len(recs)}   buys: {len(buys)}   sells: {len(sells)}")
    if not sells:
        print("no closing fills yet — nothing realized.")
        return

    realized = [float(s.get("realized_usd") or 0.0) for s in sells]
    total = sum(realized)
    wins = [x for x in realized if x > 1e-9]
    losses = [x for x in realized if x < -1e-9]
    flats = len(realized) - len(wins) - len(losses)
    gross_win, gross_loss = sum(wins), -sum(losses)
    pf = (gross_win / gross_loss) if gross_loss > 1e-9 else float("inf")

    print(f"\nrealized P&L : {fmt_usd(total)}   over {len(sells)} sell fills")
    print(f"  wins {len(wins)} / losses {len(losses)} / flat {flats}"
          f"   win-rate {100*len(wins)/len(realized):.0f}%")
    if wins:
        print(f"  avg win  {fmt_usd(gross_win/len(wins))}   gross +${gross_win:,.2f}")
    if losses:
        print(f"  avg loss {fmt_usd(-gross_loss/len(losses))}   gross -${gross_loss:,.2f}")
    print(f"  profit factor {'∞' if pf == float('inf') else f'{pf:.2f}'}"
          f"   (gross win / gross loss)")

    # --- exit-type breakdown ------------------------------------------------
    by_type: dict[str, list[float]] = defaultdict(list)
    for s, rz in zip(sells, realized):
        by_type[classify_exit(s.get("reason", ""))].append(rz)
    print(f"\n{'exit type':<22}{'n':>4}{'realized':>13}{'avg':>11}{'win%':>7}")
    print("-" * 57)
    for t in EXIT_ORDER:
        vals = by_type.get(t)
        if not vals:
            continue
        w = sum(1 for v in vals if v > 1e-9)
        print(f"{EXIT_LABEL[t]:<22}{len(vals):>4}{fmt_usd(sum(vals)):>13}"
              f"{fmt_usd(sum(vals)/len(vals)):>11}{100*w/len(vals):>6.0f}%")

    # --- scale-out spotlight ------------------------------------------------
    trims = by_type.get("scale_out", [])
    if trims:
        full_realized = total - sum(trims)
        print(f"\nscale-out ladder: {len(trims)} partial trim(s) harvested "
              f"{fmt_usd(sum(trims))}  ({fmt_usd(full_realized)} from full exits)")
    else:
        print("\nscale-out ladder: no partial trims in this window "
              "(SCALE_OUT_TIERS off, or no position cleared a tier).")

    # --- per-symbol round-trip net -----------------------------------------
    by_sym: dict[str, float] = defaultdict(float)
    for s, rz in zip(sells, realized):
        by_sym[s.get("symbol", "?")] += rz
    ranked = sorted(by_sym.items(), key=lambda kv: kv[1])
    print(f"\nper-symbol net realized ({len(ranked)} names):")
    for sym, net in ranked:
        print(f"  {sym:<8}{fmt_usd(net):>12}")


def show_open(state_path: Path) -> None:
    if not state_path.exists():
        return
    try:
        st = json.loads(state_path.read_text())
    except json.JSONDecodeError:
        return
    pos = st.get("positions") or st.get("lots") or {}   # paper_state uses "positions"; live_state "lots"
    print(f"\n{'=' * 64}\nopen positions (from {state_path.name})\n{'=' * 64}")
    if st.get("cash") is not None:
        print(f"cash ${st.get('cash', 0):,.2f}   realized_total {fmt_usd(st.get('realized_total', 0.0))}"
              f"   day {st.get('day', '?')}")
    else:   # live_state: no cash leg (broker owns it); show SOD equity instead
        print(f"start-of-day equity ${st.get('start_of_day_equity', 0) or 0:,.2f}   day {st.get('day', '?')}")
    if not pos:
        print("flat — no open positions.")
        return
    for sym, p in pos.items():
        scaled = p.get("scaled") or []
        tag = f"   scaled {scaled}" if scaled else ""
        print(f"  {sym:<8} qty {p.get('qty')}  entry {p.get('entry_price')}  "
              f"stop {p.get('stop_price')}  tp {p.get('take_profit_price')}{tag}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Realized-P&L + exit-type breakdown from the engine log.")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--state", type=Path, default=None,
                    help="state file for the open-positions block (default follows --mode: "
                         "paper_state.json for paper, live_state.json for live)")
    ap.add_argument("--since", help="ET date floor, YYYY-MM-DD (inclusive)")
    ap.add_argument("--mode", help="filter engine-log records by mode: paper / live / live-dryrun / "
                    "all. Default: $TRADING_MODE when set, else all (P4: mixed stats must be labeled)")
    ap.add_argument("--by-day", action="store_true", help="one summary block per ET trading day")
    ap.add_argument("--by-book", action="store_true",
                    help="one summary block per virtual book (pead / disco / untagged) — "
                         "the two-book split's per-cohort verdict (strategies/two-book-v2-plan.md)")
    args = ap.parse_args()

    mode = args.mode or os.environ.get("TRADING_MODE") or "all"
    print(f"MODE: {mode}" + ("  (paper + live MIXED — pass --mode live or --mode paper to split)"
                             if mode == "all" else ""))
    recs = load_records(args.log, args.since, mode)
    if not recs:
        print("no records in window.")
        return 0

    if args.by_day:
        days = sorted({r.get("ts_et", "")[:10] for r in recs})
        for d in days:
            summarize([r for r in recs if r.get("ts_et", "")[:10] == d], f"ET day {d}")
    elif args.by_book:
        books = sorted({str(res.get("book") or "untagged")
                        for _, res in iter_fills(recs) if res.get("side") == "sell"})
        for b in books or ["untagged"]:
            summarize(recs, f"BOOK: {b}", book=b)
    else:
        span = f"{recs[0].get('ts_et','')[:10]} → {recs[-1].get('ts_et','')[:10]}"
        summarize(recs, f"ALL — {span}")

    show_costs(args.since, mode, recs)
    state = args.state or (REPO / "data" / "live_state.json" if mode.startswith("live")
                           else DEFAULT_STATE)
    show_open(state)
    return 0


def show_costs(since: str | None, mode: str, recs: list[dict]) -> None:
    """Edge-vs-spend honesty line (v2 plan): gross realized P&L in the window vs LLM token cost
    from data/costs.jsonl (one row per tick, written by both executors since 2026-06-09)."""
    path = REPO / "data" / "costs.jsonl"
    if not path.exists():
        return
    dd = relay = 0.0
    n = 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since and (r.get("ts_et") or r.get("ts_utc") or "")[:10] < since:
            continue
        if mode != "all" and str(r.get("mode", "")) != mode:
            continue
        dd += float(r.get("dd_cost_usd") or 0.0)
        relay += float(r.get("relay_cost_usd") or 0.0)
        n += 1
    if not n:
        return
    realized = sum(float(res.get("realized_usd") or 0.0)
                   for _, res in iter_fills(recs) if res.get("side") == "sell")
    spend = dd + relay
    print(f"\n{'=' * 64}\nedge vs spend (window)\n{'=' * 64}")
    print(f"realized P&L {fmt_usd(realized)}   LLM spend -${spend:,.2f} "
          f"(dd ${dd:,.2f} / relay ${relay:,.2f}, {n} tick rows)")
    print(f"net of tokens: {fmt_usd(realized - spend)}"
          + ("   ← token spend exceeds gross realized edge" if spend > realized else ""))
    print("(cost ledger starts 2026-06-09 — earlier ticks aren't counted)")


if __name__ == "__main__":
    raise SystemExit(main())
