#!/usr/bin/env python3
"""
live_sentinel.py — the FAST live risk pass for FRACTIONAL lots, run every ~1 min by launchd.

Why it exists: whole-share lots are protected by a REAL resting stop_market GTC at the broker — it
sits at the exchange and fires on its own, no code needed. FRACTIONAL lots get only a SYNTHETIC stop
(a price level WE must watch). The planner tick now runs every ~5 min (cost), so between ticks a
fractional lot's synthetic stop would be unwatched. This pass closes that gap: every minute it checks
each fractional/synthetic lot's stop & take-profit against a fresh PUBLIC (Cboe) quote — NO LLM — and
fires a protective market sell via the rh_mcp relay ONLY when a level is breached (the sole LLM call,
and only on a real trigger).

It also watches the SCALE-OUT tier on ALL lots (2026-06-11): a resting stop only covers the
downside — the +10% harvest trigger is upside, visible only to the engine — so the sentinel fires
tier trims at ~1-min granularity instead of the planner's 5-min cadence, via the same unit-tested
live_execute.execute_sell partial path (cancel stop -> limit sell -> bookkeeping -> re-arm).

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
import subprocess
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
QUOTE_TAPE = REPO / "data" / "quotes-intraday.jsonl"
ET = ZoneInfo("America/New_York")

FORCE_TICK_MINUTES_ET = {(9, 32), (9, 35), (9, 39)}

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
    """A lot the sentinel must cover: no ACTUAL resting broker stop order id.
    stop_type='resting' reflects intent (set at buy time) but the real stop isn't armed until
    reconcile() confirms the fill next tick — so check the id, not the type."""
    return not lot.get("resting_stop_order_id")


def _quote_last(q: dict | None) -> float | None:
    """Live price from a quote dict — RH-parsed ({last,bid,ask}) or raw Cboe (current_price/...).
    (Fix 2026-06-11: _breach read q['last'] on raw-Cboe dicts, which is always absent, so the
    sentinel could never fire — every synthetic-stop/TP breach was silently invisible.)"""
    if not isinstance(q, dict):
        return None
    for k in ("current_price", "last"):
        v = q.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


# Pass-scoped quote cache: one batched REAL-TIME fetch per sentinel pass (see _quote).
_QUOTES: dict[str, dict] = {}


def _fetch_rh_quotes(symbols: list[str]) -> dict[str, dict]:
    """One batched fetch of the broker's own REAL-TIME marks via rh_direct (~0.3s HTTP, $0, no
    LLM) -> {SYM: {last,bid,ask}}. {} on any failure — callers fall back to per-symbol Cboe.
    Why this exists (2026-06-11): dd_probe.cboe_quote serves the *delayed* Cboe CDN feed (~15 min)
    unless the planner's quote cache is <3 min old — useless for a 1-min trigger watch. Detection
    must run on real-time data or the latency win is fictional."""
    try:
        import rh_direct
        import live_execute as le
        if not rh_direct.enabled():
            return {}
        blob = rh_direct.quotes(symbols)
        return le._parse_quotes((blob or {}).get("quotes")) or {}
    except Exception:  # noqa: BLE001 — any failure just means fallback to Cboe
        return {}


def _quote(sym: str, fresh: bool = False) -> dict:
    """Quote for one symbol: the pass's batched real-time RH marks first, per-symbol delayed Cboe
    as fallback. fresh=True forces a refetch (the 3-second confirm read before a sell)."""
    if fresh or sym not in _QUOTES:
        q = _fetch_rh_quotes([sym]).get(sym)
        _QUOTES[sym] = q if q and q.get("last") is not None else dd_probe.cboe_quote(sym)
    return _QUOTES[sym]


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
    last = _quote_last(_quote(sym))
    if not last:
        return None
    if last <= float(stop):
        reason = "synthetic_stop"
    elif tp and last >= float(tp):
        reason = "take_profit"
    else:
        return None
    # Confirm on a second read 3 s later — a single bad print cannot fire an irreversible sell.
    time.sleep(3)
    last2 = _quote_last(_quote(sym, fresh=True))
    if last2 is None:
        return None
    if reason == "synthetic_stop" and last2 > float(stop):
        return None
    if reason == "take_profit" and tp and last2 < float(tp):
        return None
    return (reason, last2)


def _effective_tiers(lot: dict) -> list[tuple[float, float]]:
    """Per-book scale-out ladder, mirroring tick_context's screen: a disco lot uses the
    DISCO_SCALE_OUT_TIERS overlay once DISCO_EXITS_LIVE=1; everything else the global ladder."""
    import tick_context as tc
    disco_on = str(os.environ.get("DISCO_EXITS_LIVE", "0")).strip().lower() \
        not in ("0", "false", "no", "")
    if disco_on and str(lot.get("book") or "disco") == "disco":
        dt = tc.scale_out_tiers("DISCO_SCALE_OUT_TIERS")
        if dt:
            return dt
    return tc.scale_out_tiers()


def _tier_breach(sym: str, lot: dict, now_s: float) -> tuple[str, float, float, list, dict] | None:
    """Return (reason, last, qty_out, gains, quote) when an untaken scale-out tier is crossed.
    Same double-read confirm as _breach. Watches ALL lots (a resting stop only covers the downside;
    the tier is an upside trigger only the engine can see), so harvests fire at 1-min granularity
    instead of waiting for the 5-min planner tick."""
    entry, qty = lot.get("entry_price"), lot.get("qty")
    if not entry or not qty:
        return None
    pend = lot.get("exit_pending_ts")
    if pend and (now_s - float(pend)) < EXIT_PENDING_COOLDOWN_S:
        return None
    tiers = _effective_tiers(lot)
    if not tiers:
        return None
    already = lot.get("scaled") or []
    if all(g in already for g, _ in tiers):
        return None
    last = _quote_last(_quote(sym))
    if not last:
        return None
    pp = (last / float(entry) - 1.0) * 100.0
    if not any(pp >= g and g not in already for g, _ in tiers):
        return None
    time.sleep(3)   # confirm read — one bad print cannot fire an irreversible sell
    q2 = _quote(sym, fresh=True)
    last2 = _quote_last(q2)
    if last2 is None:
        return None
    pp2 = (last2 / float(entry) - 1.0) * 100.0
    due = [(g, f) for g, f in tiers if pp2 >= g and g not in already]
    if not due:
        return None
    base = lot.get("init_qty") or qty
    qty_out = round(float(base) * sum(f for _, f in due), 6)
    if qty_out <= 0:
        return None
    gains = [g for g, _ in due]
    pct = int(round(sum(f for _, f in due) * 100))
    tier_lbl = ",".join(f"+{g:g}%" for g in gains)
    quote = {"last": last2, "bid": q2.get("bid"), "ask": q2.get("ask")} if isinstance(q2, dict) else {"last": last2}
    return (f"scale-out {pct}% at +{round(pp2, 2)}% (tier {tier_lbl}, sentinel)",
            last2, qty_out, gains, quote)


def main() -> int:
    ap = argparse.ArgumentParser(description="Fast live risk pass for fractional/synthetic lots.")
    ap.add_argument("--dry-run", action="store_true", help="detect + log only; fire nothing")
    args = ap.parse_args()

    if os.environ.get("TRADING_MODE", "paper") != "live" or not STATE.exists():
        return 0
    now_et = datetime.now(ET)
    _, is_open = market_conditions.session_state(now_et)
    if (now_et.hour, now_et.minute) in FORCE_TICK_MINUTES_ET and is_open:
        tick = REPO / "scripts" / "run_live_tick.sh"
        subprocess.Popen(["/bin/bash", str(tick)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        print(f"[sentinel] force-tick fired at {now_et.strftime('%H:%M')} ET")
    # Heartbeat: write before the is_open gate so off-hours runs are visible too.
    try:
        hb = REPO / "data" / "sentinel_heartbeat.txt"
        hb.write_text(datetime.now(timezone.utc).isoformat(timespec="seconds") + "\n")
    except OSError:
        pass

    if not is_open:
        return 0  # only act on a fresh regular-hours quote

    # 1) LOCK-FREE scan: read state + public quotes, find breached synthetic lots.
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return 0
    now_s = time.time()
    watched = [sym for sym, lot in (state.get("lots") or {}).items() if _needs_watch(lot)]
    print(f"[sentinel] {now_et.strftime('%H:%M:%S')} ET — watching {len(watched)} synthetic lot(s)"
          + (f": {', '.join(watched)}" if watched else ""))
    # One batched REAL-TIME quote fetch for the whole pass (broker marks via rh_direct); any
    # symbol the batch misses falls back to per-symbol Cboe inside _quote().
    _QUOTES.clear()
    _QUOTES.update({s: q for s, q in _fetch_rh_quotes(sorted(state.get("lots") or {})).items()
                    if q.get("last") is not None})  # a price-less entry must fall through to Cboe
    # Persist this pass's real-time marks (A12): the 1-min tape is the only data that can replay
    # remnant-trail variants honestly — daily bars can't see the ALOY-style intraday wick. One
    # row per pass, all held lots; consumed by exit_counterfactual.py --remnant.
    if _QUOTES:
        try:
            with QUOTE_TAPE.open("a") as f:
                f.write(json.dumps({"ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    "ts_et": now_et.isoformat(timespec="seconds"),
                                    "quotes": {s: q["last"] for s, q in _QUOTES.items()}}) + "\n")
        except OSError:
            pass
    breaches = []  # (sym, reason, last, qty)
    trims = []     # (sym, reason, last, qty_out, gains, quote)
    for sym, lot in (state.get("lots") or {}).items():
        if _needs_watch(lot):
            hit = _breach(sym, lot, now_s)
            if hit:
                breaches.append((sym, hit[0], hit[1], float(lot["qty"])))
                continue  # a full exit supersedes a trim of the same lot
        # tier trims are watched on ALL lots (resting stops only cover the downside) so the
        # harvest fires within ~1 min of the cross instead of the planner's 5-min cadence
        thit = _tier_breach(sym, lot, now_s)
        if thit:
            trims.append((sym, *thit))
    if not breaches and not trims:
        return 0

    # 2) Only now contend the shared lock (a trigger is rare). If the planner holds it, retry next min.
    if not args.dry_run:
        try:
            os.mkdir(LOCK)
        except FileExistsError:
            print(f"[sentinel] {len(breaches)} breach(es) + {len(trims)} trim(s) but planner holds "
                  "the lock — retry next pass")
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
        # 3) Tier trims — reuse live_execute.execute_sell (the unit-tested partial-sell path:
        # cancel resting stop -> price-protected limit -> mark tiers/init_qty/breakeven -> re-arm on
        # failure). The lot's `scaled` marker is the natural no-refire guard: once marked, the tier
        # is never due again; on a failed place nothing is marked and next minute retries.
        for sym, reason, last, qty_out, gains, quote in trims:
            lot = lots.get(sym)
            if not lot:
                continue
            already = lot.get("scaled") or []
            gains = [g for g in gains if g not in already]  # planner may have trimmed since the scan
            if not gains:
                continue
            if args.dry_run or not _armed():
                print(f"[sentinel] DRY — would TRIM {sym} {qty_out} ({reason} @ {last})")
                _log({"event": "sentinel_trim_dryrun", "symbol": sym, "reason": reason,
                      "last": last, "qty": qty_out, "scale_tiers": gains,
                      "ts_utc": datetime.now(timezone.utc).isoformat()})
                continue
            import live_execute as le
            caps = {"MARKETABLE_LIMIT_PCT": float(os.environ.get("MARKETABLE_LIMIT_PCT", "0.5") or 0.5)}
            broker = {"positions": {sym: {"qty": float(lot.get("qty") or 0.0)}},
                      "quotes": {sym: quote}, "orders": []}
            action = {"reason": reason, "qty": qty_out, "scale_tiers": gains}
            slog: list = []
            res = le.execute_sell(sym, action, state, broker, caps, slog)
            ok = res.get("status") == "placed"
            if ok:
                fired += 1
            print(f"[sentinel] TRIM {sym} {res.get('qty')} ({reason} @ {last}) -> {res.get('status')}")
            _log({"event": "sentinel_trim", "symbol": sym, "reason": reason, "last": last,
                  "result": {k: res.get(k) for k in ("status", "qty", "order_id", "ref_id",
                                                     "reject_reason", "realized_est_usd", "book")},
                  "ts_utc": datetime.now(timezone.utc).isoformat()})
            if ok:
                # mirror the fill to the unified trade history (best-effort, never break the pass)
                try:
                    import trade_log
                    now_utc = datetime.now(timezone.utc)
                    trade_log.record_fills([res], ts_utc=now_utc.isoformat(timespec="seconds"),
                                           ts_et=now_utc.astimezone(ET).isoformat(timespec="seconds"),
                                           mode="live")
                except Exception as e:  # noqa: BLE001
                    _log({"event": "sentinel_trim_tradelog_failed", "symbol": sym, "error": str(e),
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
