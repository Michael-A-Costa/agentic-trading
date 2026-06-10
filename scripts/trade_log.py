#!/usr/bin/env python3
"""trade_log.py — shared trade-history logging for BOTH executors (paper + live).

apply_decision.py (paper fills) and live_execute.py (live placed orders) both call
record_fills(), so every executed trade lands in ONE unified, mode-tagged history that is
independent of the fat per-tick engine-log.jsonl. The engine log answers "what did the engine
see and decide each tick"; this answers "what trades did we actually do" — a clean, greppable
audit of fills you can `grep NVDA data/trades.jsonl` to get a name's whole life.

Two artifacts are written per fill:
  data/trades.jsonl            append-only, one JSON row per executed trade (machine/queries)
  data/journal/trades-<ET>.md  human-readable daily blotter, one bullet per trade (skim it)

classify_exit() also lives here so paper, live, the blotter, the round-trip reader
(trade_ledger.py), and pnl_report.py all agree on what counts as a stop / take-profit / etc.

Single-writer safety: the tick runner holds a single-flight lock, so only one process appends
at a time; a plain append is therefore atomic enough (no interleaving). The directory is created
lazily so the first call works on a fresh checkout.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
TRADES_LOG = DATA / "trades.jsonl"
JOURNAL_DIR = DATA / "journal"

SCHEMA_VERSION = 1

# A trade is recorded only when it was actually executed: a paper "filled" or a live "placed".
# dry-run / skipped / failed orders stay in engine-log.jsonl (intent), not in the trade history.
EXECUTED_STATUSES = ("filled", "placed")

# Exit-type classifier: ordered (first match wins) substring rules against the sell `reason`.
# The strings mirror what tick_context.py emits; the [breaker-exit] prefix is stripped first.
EXIT_RULES = [
    ("scale_out",   ("scale-out",)),
    ("take_profit", ("take-profit",)),
    ("winddown",    ("wind-down",)),
    ("stop",        ("synthetic stop", "stop-loss", "stop-market", "resting stop")),
    ("eod_flatten", ("eod flatten", "flatten")),
    ("time_stop",   ("max-hold",)),
    ("test",        ("unit-test",)),
]
EXIT_LABEL = {
    "take_profit": "take-profit", "scale_out": "scale-out", "winddown": "wind-down",
    "stop": "stop-loss", "eod_flatten": "EOD flatten", "time_stop": "time-stop",
    "test": "unit-test", "other": "discretionary",
}


def classify_exit(reason: str) -> str:
    """Map a sell `reason` string to a canonical exit type (single source of truth)."""
    r = (reason or "").lower()
    if r.startswith("[breaker-exit]"):
        r = r[len("[breaker-exit]"):].strip()
    for name, needles in EXIT_RULES:
        if any(n in r for n in needles):
            return name
    return "other"


def _fill_price(res: dict) -> float | None:
    """Best-known executed price for a result from either executor.

    Paper results carry an explicit `price` (the slipped fill). Live `placed` results carry the
    order spec instead (limit for entries / marketable exits, stop_price for resting stops), so
    fall back to the spec when there's no realized fill price yet."""
    if res.get("price") is not None:
        return res["price"]
    spec = res.get("order_spec") or {}
    for k in ("limit_price", "stop_price", "price"):
        if spec.get(k) is not None:
            return spec[k]
    return None


def _fill_qty(res: dict) -> float | None:
    if res.get("qty") is not None:
        return res["qty"]
    spec = res.get("order_spec") or {}
    return spec.get("quantity")


def fill_to_trade(res: dict, *, ts_utc: str, ts_et: str | None, mode: str) -> dict:
    """Normalize one executed result (paper or live) into a compact trade row.

    Keys are included only when known, so paper and live rows stay lean and self-describing.
    """
    side = str(res.get("side", "")).lower()
    price = _fill_price(res)
    qty = _fill_qty(res)
    row: dict[str, object] = {
        "v": SCHEMA_VERSION,
        "ts_utc": ts_utc,
        "ts_et": ts_et,
        "mode": mode,
        "symbol": str(res.get("symbol", "")).upper(),
        "side": side,
        "status": res.get("status"),
        "qty": qty,
        "price": price,
        "reason": res.get("reason", ""),
    }
    # carry the fields that exist on each shape; omit the rest to keep rows tight
    for k in ("ref_price", "notional", "order_type", "stop_type", "slippage_bps",
              "order_id", "ref_id", "book"):
        if res.get(k) is not None:
            row[k] = res[k]
    if side == "buy":
        for k in ("stop_price", "take_profit_price",
                  # DD metadata (P3): pead_qualified ties the trade to the measured gap+vol signal
                  # (vs free-rein discretion) so win-rate can be split by signal class later.
                  "pead_qualified", "conviction", "hold_intent", "thesis_type"):
            if res.get(k) is not None:
                row[k] = res[k]
    else:  # sell
        if res.get("realized_usd") is not None:
            row["realized_usd"] = res["realized_usd"]
        if res.get("realized_est_usd") is not None:
            row["realized_est_usd"] = res["realized_est_usd"]  # flagged estimate (live place-time)
        if res.get("scale_tiers") is not None:
            row["scale_tiers"] = res["scale_tiers"]
        row["exit_type"] = classify_exit(res.get("reason", ""))
    return row


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _fmt_usd(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _blotter_line(row: dict) -> str:
    """One human-readable markdown bullet for a trade row."""
    t = (row.get("ts_et") or row.get("ts_utc") or "")[11:19]  # HH:MM:SS
    side = str(row.get("side", "")).upper()
    qty = row.get("qty")
    price = row.get("price")
    px = f"@ {price}" if price is not None else "@ ?"
    extra = []
    if row.get("side") == "buy":
        if row.get("stop_price") is not None:
            extra.append(f"stop {row['stop_price']}")
        if row.get("take_profit_price") is not None:
            extra.append(f"tp {row['take_profit_price']}")
        if row.get("stop_type"):
            extra.append(str(row["stop_type"]))
        if row.get("pead_qualified") is True:
            extra.append("PEAD✓")
        if row.get("book"):
            extra.append(f"book:{row['book']}")
    else:
        if row.get("realized_usd") is not None:
            extra.append(_fmt_usd(float(row["realized_usd"])))
        if row.get("exit_type"):
            extra.append(EXIT_LABEL.get(row["exit_type"], row["exit_type"]))
    tag = f"  ({', '.join(extra)})" if extra else ""
    reason = str(row.get("reason") or "").strip()
    rtxt = f"  — {reason[:80]}" if reason else ""
    # placed != filled (P6): a live "placed" order is an INTENT until confirmed; "dead" never filled.
    status = str(row.get("status") or "")
    flag = {"filled": "", "placed": "  [placed — fill unconfirmed]",
            "dead": "  [NOT FILLED]"}.get(status, f"  [{status}]" if status else "")
    return f"- `{t}` **{side}** {qty} {row.get('symbol')} {px}{tag}{flag}{rtxt}"


def _append_blotter(rows: list[dict]) -> None:
    """Append bullets to the per-ET-day markdown blotter (one file per trading day)."""
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        day = (r.get("ts_et") or r.get("ts_utc") or "unknown")[:10]
        by_day.setdefault(day, []).append(r)
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    for day, day_rows in by_day.items():
        path = JOURNAL_DIR / f"trades-{day}.md"
        new = not path.exists()
        with path.open("a") as f:
            if new:
                f.write(f"# Trade blotter — {day}\n\n"
                        "Auto-written per fill by `trade_log.py`. One bullet = one executed trade "
                        "(paper fill or live placed order). See `data/trades.jsonl` for the "
                        "machine-readable rows and `scripts/trade_ledger.py` for round-trips.\n\n")
            for r in day_rows:
                f.write(_blotter_line(r) + "\n")


def record_reconcile_events(events: list[dict], *, ts_utc: str, ts_et: str | None,
                            mode: str) -> list[dict]:
    """Book reconcile outcomes into the trade history (P6: placed != filled).

    Converts live_execute.reconcile() log events into trade rows so the blotter and stats agree
    with broker truth:
      entry_filled_confirmed              -> buy  status=filled (a prior tick's placed order filled)
      entry_unfilled / _cancelled         -> buy  status=dead   (placed order never filled — GFD
                                                                 expired / marketable limit missed)
      closed_external                     -> sell status=filled, price unknown (resting stop fired
                                                                 or sold while the engine slept)
    Rows carry order_id where known; consumers dedupe placed/terminal pairs by order_id.
    Best-effort like record_fills — never crash a tick."""
    rows: list[dict] = []
    for ev in (events or []):
        kind = ev.get("event")
        sym = str(ev.get("symbol", "")).upper()
        if not sym:
            continue
        if kind == "entry_filled_confirmed":
            rows.append({"v": SCHEMA_VERSION, "ts_utc": ts_utc, "ts_et": ts_et, "mode": mode,
                         "symbol": sym, "side": "buy", "status": "filled",
                         "qty": ev.get("qty"), "price": ev.get("avg_cost"),
                         "order_id": ev.get("order_id"),
                         "reason": "fill confirmed by reconcile (placed on a prior tick)"})
        elif kind in ("entry_unfilled", "entry_unfilled_cancelled"):
            rows.append({"v": SCHEMA_VERSION, "ts_utc": ts_utc, "ts_et": ts_et, "mode": mode,
                         "symbol": sym, "side": "buy", "status": "dead",
                         "qty": None, "price": None, "order_id": ev.get("order_id"),
                         "reason": "entry never filled (GFD expired / marketable limit missed)"})
        elif kind == "closed_external":
            rows.append({"v": SCHEMA_VERSION, "ts_utc": ts_utc, "ts_et": ts_et, "mode": mode,
                         "symbol": sym, "side": "sell", "status": "filled",
                         "qty": ev.get("qty"), "price": None,
                         "reason": ev.get("note", "position closed at broker while engine asleep"),
                         **({"book": ev["book"]} if ev.get("book") else {}),
                         **({"realized_est_usd": ev["realized_est_usd"]}
                            if ev.get("realized_est_usd") is not None else {}),
                         "exit_type": "stop"})
    if rows:
        try:
            _append_jsonl(TRADES_LOG, rows)
            _append_blotter(rows)
        except OSError:
            pass
    return rows


def record_fills(results: list[dict], *, ts_utc: str, ts_et: str | None, mode: str) -> list[dict]:
    """Record every EXECUTED trade from a tick's results to the unified history.

    Filters `results` to executed fills, writes one row each to data/trades.jsonl and a bullet to
    the day's markdown blotter, and returns the rows (for the caller's summary, if wanted).
    Best-effort: trade history must never crash a tick, so any I/O error is swallowed after the
    rows are built — the authoritative engine-log write still happens in the executor.
    """
    executed = [r for r in (results or []) if r.get("status") in EXECUTED_STATUSES]
    rows = [fill_to_trade(r, ts_utc=ts_utc, ts_et=ts_et, mode=mode) for r in executed]
    if not rows:
        return []
    try:
        _append_jsonl(TRADES_LOG, rows)
        _append_blotter(rows)
    except OSError:
        pass
    return rows
