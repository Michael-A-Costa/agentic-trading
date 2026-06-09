#!/usr/bin/env python3
"""
live_sentinel.py — the FAST live risk pass for FRACTIONAL lots, run every ~1 min by launchd.

Why it exists: whole-share lots are protected by a REAL resting stop_market GTC at the broker — it
sits at the exchange and fires on its own, no code needed. FRACTIONAL lots get only a SYNTHETIC stop
(a price level WE must watch). The planner tick now runs every ~10 min (cost), so between ticks a
fractional lot's synthetic stop would be unwatched. This pass closes that gap: every minute it checks
each fractional/synthetic lot's stop & take-profit against a fresh PUBLIC (Cboe) quote — NO LLM — and
fires a protective market sell via the rh_mcp relay ONLY when a level is breached (the sole LLM call,
and only on a real trigger).

Design (so it doesn't fight the planner):
  - It READS live_state.json + public quotes lock-free (a slightly stale read is harmless — a sell of
    an already-closed lot just rejects at the broker). It only acquires the shared data/.tick.lock to
    WRITE (fire a sell + update state); if the planner holds it, the breach persists and is retried
    next minute.
  - exit_pending stamps a fired lot so a slow/failed relay isn't re-fired every minute; the planner's
    reconcile books the real fill from broker truth and removes the lot.

Usage:  live_sentinel.py            # one fast pass
        live_sentinel.py --dry-run  # detect + log intended sells, fire NOTHING
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dd_probe            # noqa: E402  cboe_quote — public, no-LLM
import market_conditions   # noqa: E402  session_state

REPO = Path(__file__).resolve().parent.parent
STATE = REPO / "data" / "live_state.json"
LOCK = REPO / "data" / ".tick.lock"
ENGINE_LOG = REPO / "data" / "engine-log.jsonl"
ET = ZoneInfo("America/New_York")

# don't re-fire a lot whose sell was already dispatched within this window (lets the planner reconcile
# book the fill before we'd try again); after it, a still-held + still-breached lot may re-fire.
EXIT_PENDING_COOLDOWN_S = 180


def _armed() -> bool:
    return str(os.environ.get("LIVE_ARMED", "0")).strip().lower() in ("1", "true", "yes", "on")


def _log(rec: dict) -> None:
    try:
        ENGINE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ENGINE_LOG.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def _needs_watch(lot: dict) -> bool:
    """A lot the sentinel must cover: NO live resting broker stop (fractional, or a whole-share lot
    whose resting stop failed to arm and degraded to synthetic)."""
    return not lot.get("resting_stop_order_id") and lot.get("stop_type") != "resting"


def _breach(sym: str, lot: dict, now_s: float) -> tuple[str, float] | None:
    """Return (reason, last_price) if this lot's synthetic stop or take-profit is hit, else None."""
    stop = lot.get("stop_price")
    tp = lot.get("take_profit_price")
    qty = lot.get("qty")
    if not stop or not qty:
        return None
    pend = lot.get("exit_pending_ts")
    if pend and (now_s - float(pend)) < EXIT_PENDING_COOLDOWN_S:
        return None  # a sell is already in flight for this lot
    q = dd_probe.cboe_quote(sym)
    last = q.get("last") if isinstance(q, dict) else None
    if not last:
        return None
    last = float(last)
    if last <= float(stop):
        return ("synthetic_stop", last)
    if tp and last >= float(tp):
        return ("take_profit", last)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Fast live risk pass for fractional/synthetic lots.")
    ap.add_argument("--dry-run", action="store_true", help="detect + log only; fire nothing")
    args = ap.parse_args()

    if os.environ.get("TRADING_MODE", "paper") != "live" or not STATE.exists():
        return 0
    _, is_open = market_conditions.session_state(datetime.now(ET))
    if not is_open:
        return 0  # only act on a fresh regular-hours quote

    # 1) LOCK-FREE scan: read state + public quotes, find breached synthetic lots.
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return 0
    now_s = time.time()
    breaches = []  # (sym, reason, last, qty)
    for sym, lot in (state.get("lots") or {}).items():
        if not _needs_watch(lot):
            continue
        hit = _breach(sym, lot, now_s)
        if hit:
            breaches.append((sym, hit[0], hit[1], float(lot["qty"])))
    if not breaches:
        return 0

    # 2) Only now contend the shared lock (a breach is rare). If the planner holds it, retry next min.
    if not args.dry_run:
        try:
            os.mkdir(LOCK)
        except FileExistsError:
            print(f"[sentinel] {len(breaches)} breach(es) but planner holds the lock — retry next pass")
            return 0
    try:
        # re-read state under the lock (the planner may have changed it since the lock-free scan)
        if not args.dry_run:
            try:
                state = json.loads(STATE.read_text())
            except (OSError, ValueError):
                return 0
        lots = state.get("lots") or {}
        rh_mcp = None
        fired = 0
        for sym, reason, last, _qty in breaches:
            lot = lots.get(sym)
            if not lot or not _needs_watch(lot):
                continue  # planner already handled it
            spec = {"symbol": sym, "side": "sell", "type": "market",
                    "quantity": f"{float(lot['qty']):.6f}".rstrip("0").rstrip("."),
                    "time_in_force": "gfd"}
            if args.dry_run or not _armed():
                print(f"[sentinel] DRY — would SELL {sym} ({reason} @ {last}, stop={lot.get('stop_price')})")
                _log({"event": "sentinel_exit_dryrun", "symbol": sym, "reason": reason,
                      "last": last, "spec": spec, "ts_utc": datetime.now(timezone.utc).isoformat()})
                continue
            if rh_mcp is None:
                import rh_mcp as _rh
                rh_mcp = _rh
            ref = str(uuid.uuid4())
            placed = rh_mcp.place(spec, ref_id=ref)
            ok = isinstance(placed, dict) and placed.get("order") is not None
            lot["exit_pending_ts"] = now_s   # stop re-firing; planner reconcile books the real fill
            lot["exit_reason"] = reason
            fired += 1
            print(f"[sentinel] SELL {sym} ({reason} @ {last}) -> {'placed' if ok else 'relay-uncertain (planner will reconcile)'}")
            _log({"event": "sentinel_exit", "symbol": sym, "reason": reason, "last": last,
                  "spec": spec, "ref_id": ref, "placed_ok": ok,
                  "ts_utc": datetime.now(timezone.utc).isoformat()})
        if fired and not args.dry_run:
            tmp = STATE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state))
            os.replace(tmp, STATE)
    finally:
        if not args.dry_run:
            try:
                os.rmdir(LOCK)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
