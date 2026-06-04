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
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_LOG = REPO / "data" / "engine-log.jsonl"
DEFAULT_STATE = REPO / "data" / "paper_state.json"

# Exit-type classifier: ordered (first match wins) substring rules against the sell `reason`.
# The strings mirror what tick_context.py emits; the [breaker-exit] prefix is stripped first.
EXIT_RULES = [
    ("scale_out",   ("scale-out",)),
    ("take_profit", ("take-profit",)),
    ("winddown",    ("wind-down",)),
    ("stop",        ("synthetic stop", "stop-loss")),
    ("eod_flatten", ("eod flatten",)),
    ("time_stop",   ("max-hold",)),
    ("test",        ("unit-test",)),
]
# Display order + labels for the breakdown table.
EXIT_ORDER = ["take_profit", "scale_out", "winddown", "stop", "eod_flatten", "time_stop", "test", "other"]
EXIT_LABEL = {
    "take_profit": "take-profit (full)", "scale_out": "scale-out (partial)",
    "winddown": "EOD wind-down (green)",
    "stop": "stop-loss", "eod_flatten": "EOD flatten", "time_stop": "time-stop (stalled)",
    "test": "unit-test", "other": "other/discretionary",
}


def classify_exit(reason: str) -> str:
    r = (reason or "").lower()
    if r.startswith("[breaker-exit]"):
        r = r[len("[breaker-exit]"):].strip()
    for name, needles in EXIT_RULES:
        if any(n in r for n in needles):
            return name
    return "other"


def load_records(log_path: Path, since: str | None) -> list[dict]:
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


def summarize(recs: list[dict], title: str) -> None:
    sells = [res for _, res in iter_fills(recs) if res.get("side") == "sell"]
    buys = [res for _, res in iter_fills(recs) if res.get("side") == "buy"]

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
    pos = st.get("positions", {})
    print(f"\n{'=' * 64}\nopen positions (from {state_path.name})\n{'=' * 64}")
    print(f"cash ${st.get('cash', 0):,.2f}   realized_total {fmt_usd(st.get('realized_total', 0.0))}"
          f"   day {st.get('day', '?')}")
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
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--since", help="ET date floor, YYYY-MM-DD (inclusive)")
    ap.add_argument("--by-day", action="store_true", help="one summary block per ET trading day")
    args = ap.parse_args()

    recs = load_records(args.log, args.since)
    if not recs:
        print("no records in window.")
        return 0

    if args.by_day:
        days = sorted({r.get("ts_et", "")[:10] for r in recs})
        for d in days:
            summarize([r for r in recs if r.get("ts_et", "")[:10] == d], f"ET day {d}")
    else:
        span = f"{recs[0].get('ts_et','')[:10]} → {recs[-1].get('ts_et','')[:10]}"
        summarize(recs, f"ALL — {span}")

    show_open(args.state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
