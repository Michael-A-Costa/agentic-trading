#!/usr/bin/env python3
"""
live_execute.py — the LIVE executor (real-money counterpart to apply_decision.py).

Reads the broker snapshot (data/tick/broker_snapshot.json, written by broker_snapshot.py) and the
LLM decision (data/tick/decision_latest.json), then turns each action into real MCP orders via the
rh_mcp relay. ALL sizing / cap / gating logic is here in Python; the relay agent only executes a
precise recipe and echoes JSON. Truth is the broker — local live_state.json holds ONLY the metadata
the broker doesn't track (our stop/TP levels, entry_ts, scale-out progress, resting-stop order id).

Order semantics (pinned by the MCP schema — dollar_amount / fractional are market-only; limit and
stop_market need whole-share quantity):
  - whole-share BUY  -> type=limit  (marketable, capped MARKETABLE_LIMIT_PCT above the ask)
                        + resting type=stop_market (GTC) armed at the broker once the fill confirms
  - fractional BUY   -> type=market (dollar_amount); SYNTHETIC engine-tick stop only (no broker stop)
  - SELL (exit/scale)-> cancel any resting stop first, then limit (whole) / market (fractional)

Safety gate (no human per-trade approval — this IS the seatbelt):
  - account hard-pinned to AGENTIC_ACCOUNT
  - every BUY: review_equity_order -> Python inspects alerts -> place only if clear
  - LIVE_ARMED!=1 => DRY-RUN: review + log "would place", never place
  - canary: first armed order capped to LIVE_CANARY_USD until one live round-trip completes
  - caps re-checked against fresh broker buying power; daily breaker on broker start-of-day equity
  - ref_id idempotency per logical order

NOTE: the exact JSON field names returned by the RH read tools are not known until a live dry-run;
the parse_* helpers try the likely keys and fail safe. Lock them down after the first dry-run.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trade_log  # shared trade-history writer (paper + live)

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
SNAPSHOT_PATH = DATA / "tick" / "broker_snapshot.json"
STATE_PATH = DATA / "live_state.json"
ENGINE_LOG = DATA / "engine-log.jsonl"


# ---------------------------------------------------------------------------
# small utils
# ---------------------------------------------------------------------------
def load_json(p: Path) -> dict:
    return json.loads(p.read_text())


def write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def _f(x, default=None):
    """Coerce a possibly-string broker number to float; None/'' -> default."""
    if x is None or x == "":
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def armed() -> bool:
    return str(os.environ.get("LIVE_ARMED", "0")).strip().lower() in ("1", "true", "yes", "on")


def _first(d: dict, *keys, default=None):
    """Return the first present, non-None key from a dict (defensive broker-field mapping)."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


# ---------------------------------------------------------------------------
# broker snapshot parsing  (ASSUMED field shapes — confirm/adjust after first dry-run)
# ---------------------------------------------------------------------------
def _unwrap(x):
    """Tool results come wrapped as {"data": {...}, "guide": "..."}. Peel the data envelope if the
    relay agent echoed it verbatim; pass through if it already handed us the inner object."""
    return x["data"] if isinstance(x, dict) and isinstance(x.get("data"), dict) else x


def order_obj(placed: dict | None) -> dict | None:
    """Extract the order object from a rh_mcp.place result ({"order": <verbatim>, "errors": ...}),
    peeling the {"data": {...}} envelope. Returns None if the relay errored or shape is unusable."""
    if not isinstance(placed, dict):
        return None
    o = _unwrap(placed.get("order", placed))
    return o if isinstance(o, dict) else None


def parse_snapshot(snap: dict) -> dict:
    """Normalise the raw broker blobs into {buying_power, positions{sym:{qty,avg_cost}}, quotes, orders}.

    Field shapes confirmed against live MCP output 2026-06-04:
      get_portfolio       -> data.buying_power.buying_power (nested!) / data.cash
      get_equity_positions-> data.positions[].{quantity, average_buy_price, shares_available_for_sells}
      get_equity_quotes   -> data.results[].quote.{bid_price, ask_price, last_trade_price, last_non_reg_trade_price}
      get_equity_orders   -> data.orders[].{id, symbol, side, type, state, stop_price, ...}
    """
    pf = _unwrap(snap.get("portfolio") or {})
    bp = pf.get("buying_power") if isinstance(pf, dict) else None
    if isinstance(bp, dict):  # nested {"buying_power": "1064.0000", "unleveraged_buying_power": ...}
        buying_power = _f(_first(bp, "buying_power", "unleveraged_buying_power"), None)
    else:
        buying_power = _f(bp, None)
    if buying_power is None:
        buying_power = _f(_first(pf, "cash", "buying_power_usd", "cash_available_for_trading"), 0.0)

    raw_pos = _unwrap(snap.get("positions") or {})
    if isinstance(raw_pos, dict):
        raw_pos = raw_pos.get("positions") or raw_pos.get("results") or []
    positions: dict[str, dict] = {}
    for p in raw_pos or []:
        if not isinstance(p, dict):
            continue
        sym = (_first(p, "symbol", "ticker", "instrument_symbol") or "").upper().strip()
        qty = _f(_first(p, "quantity", "qty", "shares"), 0.0) or 0.0
        if not sym or qty <= 0:
            continue
        avg = _f(_first(p, "average_buy_price", "average_price", "cost_basis_per_share", "avg_cost"), None)
        sellable = _f(_first(p, "shares_available_for_sells"), qty)
        positions[sym] = {"qty": qty, "avg_cost": avg, "sellable": sellable}

    raw_q = _unwrap(snap.get("quotes") or {})
    if isinstance(raw_q, dict):
        raw_q = raw_q.get("results") or raw_q.get("quotes") or []
    quotes: dict[str, dict] = {}
    for item in raw_q or []:
        q = item.get("quote") if isinstance(item, dict) and isinstance(item.get("quote"), dict) else item
        if not isinstance(q, dict):
            continue
        sym = (_first(q, "symbol", "ticker") or "").upper().strip()
        if sym:
            quotes[sym] = {"bid": _f(_first(q, "bid_price", "bid")), "ask": _f(_first(q, "ask_price", "ask")),
                           "last": _f(_first(q, "last_trade_price", "last_non_reg_trade_price", "last", "price"))}

    raw_o = _unwrap(snap.get("orders") or {})
    if isinstance(raw_o, dict):
        raw_o = raw_o.get("orders") or raw_o.get("results") or []
    orders = [o for o in (raw_o or []) if isinstance(o, dict)]
    return {"buying_power": buying_power, "positions": positions, "quotes": quotes, "orders": orders}


def open_stop_for(orders: list, sym: str) -> dict | None:
    """Find an OPEN resting protective stop (sell) for a symbol. A Robinhood stop comes back with a
    non-null stop_price (type may read 'market'/'limit' with trigger='stop'), so we key on stop_price
    + side, NOT type=='stop_market'."""
    OPEN = {"new", "queued", "confirmed", "unconfirmed", "partially_filled"}
    for o in orders:
        osym = (_first(o, "symbol", "ticker") or "").upper().strip()
        oside = (_first(o, "side") or "").lower()
        ostate = (_first(o, "state", "status") or "").lower()
        has_stop = _first(o, "stop_price") is not None or "stop" in (_first(o, "type", "trigger", "order_type") or "").lower()
        if osym == sym and oside == "sell" and has_stop and ostate in OPEN:
            return o
    return None


# ---------------------------------------------------------------------------
# PURE order-spec builders + cap checks  (unit-tested; no MCP, no I/O)
# ---------------------------------------------------------------------------
def size_entry(dollar_amount: float, quote: dict, caps: dict, *, canary_usd: float | None) -> dict:
    """Decide qty / order kind for a BUY. Returns a plan dict (no MCP call).

    Mirrors the paper hybrid: floor to whole shares when affordable (-> limit + resting stop),
    else fractional market (-> synthetic stop). Honours the canary notional cap when set.
    """
    ref = quote.get("ask") or quote.get("last") or quote.get("bid")
    if not ref or ref <= 0:
        return {"ok": False, "reject_reason": "no usable quote (ask/last) for sizing"}
    notional = float(dollar_amount)
    if canary_usd is not None and notional > canary_usd:
        notional = canary_usd  # first live order is a tiny canary until a round-trip completes
    prefer_whole = str(os.environ.get("PREFER_WHOLE_SHARES", "1")).strip().lower() not in ("0", "false", "no", "")
    limit_pct = caps.get("MARKETABLE_LIMIT_PCT", 0.5)
    raw_qty = notional / ref
    whole = prefer_whole and math.floor(raw_qty) >= 1
    if whole:
        qty = float(math.floor(raw_qty))
        limit_price = round(ref * (1 + limit_pct / 100.0), 2)
        notional = qty * limit_price
        return {"ok": True, "kind": "limit", "qty": qty, "whole": True, "stop_type": "resting",
                "limit_price": limit_price, "notional": round(notional, 2),
                "canary_capped": canary_usd is not None and float(dollar_amount) > canary_usd}
    # fractional -> market dollar order, synthetic stop only
    return {"ok": True, "kind": "market", "dollar_amount": round(notional, 2), "whole": False,
            "stop_type": "synthetic", "qty": round(raw_qty, 6), "notional": round(notional, 2),
            "canary_capped": canary_usd is not None and float(dollar_amount) > canary_usd}


def check_entry_caps(plan: dict, *, existing_val: float, exposure: float,
                     buying_power: float, n_positions: int, held: bool, caps: dict,
                     day_pnl: float | None) -> tuple[bool, str]:
    """Re-check every .env cap against FRESH broker numbers. Mirrors apply_decision.validate_and_fill
    (scripts/apply_decision.py:151-192) — keep the two in sync."""
    notional = plan["notional"]
    min_pos = caps.get("MIN_POSITION_USD", 0.0)
    if min_pos > 0 and notional < min_pos - 1e-6:
        return False, f"below MIN_POSITION_USD ({min_pos})"
    if existing_val + notional > caps["MAX_POSITION_USD"] + 1e-6:
        return False, f"exceeds MAX_POSITION_USD ({caps['MAX_POSITION_USD']})"
    if exposure + notional > caps["MAX_TOTAL_EXPOSURE_USD"] + 1e-6:
        return False, f"exceeds MAX_TOTAL_EXPOSURE_USD ({caps['MAX_TOTAL_EXPOSURE_USD']})"
    if not held and n_positions >= caps["MAX_OPEN_POSITIONS"]:
        return False, f"MAX_OPEN_POSITIONS ({caps['MAX_OPEN_POSITIONS']}) reached"
    implied_stop_loss = notional * caps["STOP_LOSS_PCT"] / 100.0
    max_trade_loss = caps.get("MAX_PER_TRADE_LOSS_USD", 60.0)
    if implied_stop_loss > max_trade_loss + 1e-6:
        return False, f"exceeds MAX_PER_TRADE_LOSS_USD: {round(implied_stop_loss, 2)} > {max_trade_loss}"
    if day_pnl is not None and day_pnl <= -caps.get("DAILY_MAX_LOSS_USD", 150.0):
        return False, f"circuit_breaker day_pnl={round(day_pnl, 2)} <= -{caps.get('DAILY_MAX_LOSS_USD')}"
    if notional > buying_power + 1e-6:
        return False, f"insufficient buying power ({round(buying_power, 2)})"
    return True, ""


def buy_spec(sym: str, plan: dict) -> dict:
    """Build the MCP place_equity_order params for a BUY from a sized plan."""
    if plan["kind"] == "limit":
        return {"symbol": sym, "side": "buy", "type": "limit", "quantity": str(int(plan["qty"])),
                "limit_price": f"{plan['limit_price']:.2f}", "time_in_force": "gfd",
                "market_hours": "regular_hours"}
    return {"symbol": sym, "side": "buy", "type": "market",
            "dollar_amount": f"{plan['dollar_amount']:.2f}", "market_hours": "regular_hours"}


def stop_spec(sym: str, qty: float, stop_price: float) -> dict:
    """Resting protective stop-market (whole-share lots only). GTC so it survives between ticks."""
    return {"symbol": sym, "side": "sell", "type": "stop_market", "quantity": str(int(qty)),
            "stop_price": f"{stop_price:.2f}", "time_in_force": "gtc"}


def sell_spec(sym: str, qty: float, *, whole: bool, quote: dict, caps: dict) -> dict:
    """Discretionary/exit sell: marketable limit for a whole-share lot, market for a fractional one."""
    if whole and float(qty) == math.floor(float(qty)):
        ref = quote.get("bid") or quote.get("last") or quote.get("ask")
        limit_pct = caps.get("MARKETABLE_LIMIT_PCT", 0.5)
        if ref and ref > 0:
            lp = round(ref * (1 - limit_pct / 100.0), 2)
            return {"symbol": sym, "side": "sell", "type": "limit", "quantity": str(int(qty)),
                    "limit_price": f"{lp:.2f}", "time_in_force": "gfd", "market_hours": "regular_hours"}
    # fractional, or no quote to anchor a limit -> market sell (fractional is market-only anyway)
    q = str(int(qty)) if float(qty) == math.floor(float(qty)) else f"{float(qty):.6f}"
    return {"symbol": sym, "side": "sell", "type": "market", "quantity": q, "market_hours": "regular_hours"}


# Alert types that should STOP an order. Confirmed shape 2026-06-04: review.data.order_checks is {}
# when clear, else an object carrying an "alertType". A clean order still returns EQUITY_SUITABILITY
# (a routine individual-account disclosure) — so we do NOT block on every alert, only on a denylist
# of genuinely-bad conditions. Tighten this as new alertTypes are observed in the wild.
BLOCKING_ALERT_KEYWORDS = ("BUYING_POWER", "INSUFFICIENT", "PDT", "PATTERN_DAY", "DAY_TRADE",
                           "HALT", "RESTRICT", "SUSPEND", "REJECT", "UNSETTLED", "COLLATERAL",
                           "MARGIN_CALL", "UNTRADAB", "NOT_TRADAB", "BLOCK", "DENIED")


def review_blocking(review_payload: dict | None) -> list:
    """Return BLOCKING alert types from a review_equity_order result. Reads data.order_checks ({} =
    clear). Blocks only on the denylist above; routine disclosures (EQUITY_SUITABILITY) pass.
    Unparseable / relay error -> block (fail-safe — never place on a review we can't read)."""
    if not isinstance(review_payload, dict):
        return ["review_unparseable"]
    if review_payload.get("errors"):
        return [f"relay_error:{review_payload['errors']}"]
    rv = _unwrap(review_payload.get("review", review_payload))
    if not isinstance(rv, dict):
        return ["review_unparseable"]
    checks = rv.get("order_checks")
    if not checks:  # {} or None => no broker alerts
        return []
    items = checks if isinstance(checks, list) else [checks]
    blocking = []
    for it in items:
        atype = (str(it.get("alertType") or it.get("alert_type") or it.get("type") or "")
                 if isinstance(it, dict) else str(it)).upper()
        if any(k in atype for k in BLOCKING_ALERT_KEYWORDS):
            blocking.append(atype)
    return blocking


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            bak = STATE_PATH.with_suffix(f".corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json")
            try:
                os.replace(STATE_PATH, bak)
            except OSError:
                pass
            print(f"[live_execute] FATAL: live_state.json unreadable; backed up to {bak.name}", file=sys.stderr)
            raise
    return {"lots": {}, "day": None, "start_of_day_equity": None, "live_round_trip_done": False}


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
def reconcile(state: dict, broker: dict, log: list) -> None:
    """Make local metadata agree with broker truth: confirm fills, arm missing stops, book closures."""
    import rh_mcp  # local import so the pure builders stay importable without the MCP runner
    lots = state.setdefault("lots", {})
    bpos = broker["positions"]
    orders = broker["orders"]
    do_arm = armed()

    # 1) positions the broker holds: refresh qty/entry, map/arm the resting stop on whole-share lots.
    for sym, bp in bpos.items():
        lot = lots.setdefault(sym, {"entry_ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    "scaled": [], "stop_type": "synthetic", "resting_stop_order_id": None,
                                    "adopted": sym not in lots})
        lot["qty"] = bp["qty"]
        lot.pop("pending", None)  # broker shows the position -> entry confirmed, no longer pending
        if bp.get("avg_cost"):
            lot["entry_price"] = bp["avg_cost"]
        sl = state.get("_caps", {}).get("STOP_LOSS_PCT", 4.0)
        tp = state.get("_caps", {}).get("TAKE_PROFIT_PCT", 12.0)
        if lot.get("entry_price"):
            lot["stop_price"] = round(lot["entry_price"] * (1 - sl / 100.0), 4)
            lot["take_profit_price"] = round(lot["entry_price"] * (1 + tp / 100.0), 4)
        # map an existing broker resting stop to the lot
        existing = open_stop_for(orders, sym)
        if existing:
            lot["resting_stop_order_id"] = _first(existing, "id", "order_id")
            lot["stop_type"] = "resting"
        whole = float(bp["qty"]) == math.floor(float(bp["qty"])) and bp["qty"] >= 1
        # arm a resting stop on a whole-share lot that has a confirmed entry but no live stop yet
        if whole and not lot.get("resting_stop_order_id") and lot.get("entry_price"):
            if do_arm:
                res = rh_mcp.place(stop_spec(sym, math.floor(bp["qty"]), lot["stop_price"]),
                                   ref_id=str(uuid.uuid4()))
                o = order_obj(res)
                oid = _first(o, "id", "order_id") if isinstance(o, dict) else None
                lot["resting_stop_order_id"] = oid
                lot["stop_type"] = "resting" if oid else "synthetic"  # fall back to synthetic if arm failed
                log.append({"event": "arm_stop" if oid else "arm_stop_failed", "symbol": sym,
                            "stop_price": lot["stop_price"], "order_id": oid,
                            "qty": math.floor(bp["qty"]), "result": None if oid else res})
            else:
                lot["stop_type"] = "synthetic"  # dry-run: rely on the engine-tick synthetic stop
                log.append({"event": "arm_stop_dryrun", "symbol": sym, "stop_price": lot.get("stop_price")})

    # 2) lots we track but the broker no longer holds. Two cases:
    #    a) a PENDING entry from a prior tick that never showed as a position -> it didn't fill
    #       (marketable limit gapped away / GFD expired). Cancel any still-open entry order and drop
    #       the lot — do NOT book it as a closed position (it was never opened), and do NOT count it
    #       as a round-trip.
    #    b) a position we actually held that's now gone -> a real closure (stop fired / sold while
    #       asleep). Record the exit; if it was a lot we entered ourselves (not adopted), one full
    #       live round-trip is now complete, so the canary notional cap can lift.
    for sym in list(lots.keys()):
        if sym not in bpos:
            stale = lots.pop(sym)
            if stale.get("pending"):
                eoid = stale.get("entry_order_id")
                if eoid and do_arm:
                    c = rh_mcp.cancel(eoid)
                    log.append({"event": "entry_unfilled_cancelled", "symbol": sym,
                                "order_id": eoid, "result": c})
                else:
                    log.append({"event": "entry_unfilled", "symbol": sym, "order_id": eoid})
                continue
            state.setdefault("last_exit", {})[sym] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            log.append({"event": "closed_external", "symbol": sym,
                        "note": "position gone from broker — resting stop fired or sold while engine asleep",
                        "had_stop": stale.get("resting_stop_order_id")})
            if not stale.get("adopted"):
                state["live_round_trip_done"] = True


def execute_sell(sym: str, action: dict, state: dict, broker: dict, caps: dict, log: list) -> dict:
    """Cancel any resting stop, then sell. Returns a result record."""
    import rh_mcp
    lots = state["lots"]
    res = {"symbol": sym, "side": "sell", "reason": action.get("reason", ""), "status": "skipped"}
    if sym not in broker["positions"]:
        res["reject_reason"] = "no broker position to sell"
        return res
    lot = lots.get(sym, {})
    held = broker["positions"][sym]["qty"]
    qty = float(action["qty"]) if action.get("qty") is not None else held
    qty = min(qty, held)
    if qty <= 0:
        res["reject_reason"] = "non-positive sell qty"
        return res
    whole = float(qty) == math.floor(float(qty)) and qty >= 1
    spec = sell_spec(sym, qty, whole=whole, quote=broker["quotes"].get(sym, {}), caps=caps)
    res.update(order_spec=spec, qty=qty)

    if not armed():
        res["status"] = "dryrun"
        log.append({"event": "sell_dryrun", "symbol": sym, "spec": spec,
                    "cancel_stop": lot.get("resting_stop_order_id")})
        return res
    # cancel resting stop first so the shares are free to sell
    if lot.get("resting_stop_order_id"):
        c = rh_mcp.cancel(lot["resting_stop_order_id"])
        log.append({"event": "cancel_stop", "symbol": sym, "order_id": lot["resting_stop_order_id"],
                    "result": c})
        lot["resting_stop_order_id"] = None
    ref_id = str(uuid.uuid4())
    placed = rh_mcp.place(spec, ref_id=ref_id)
    o = order_obj(placed)
    order_id = _first(o, "id", "order_id") if isinstance(o, dict) else None
    if not order_id or (isinstance(placed, dict) and placed.get("errors")):
        res.update(status="failed", ref_id=ref_id, reject_reason=f"sell rejected/failed: {placed}", order=placed)
        log.append({"event": "sell_failed", "symbol": sym, "spec": spec, "ref_id": ref_id, "result": placed})
        return res
    res.update(status="placed", ref_id=ref_id, order_id=order_id, order=placed)
    log.append({"event": "sell_placed", "symbol": sym, "spec": spec, "ref_id": ref_id,
                "order_id": order_id, "order": placed})
    return res


def execute_buy(sym: str, action: dict, state: dict, broker: dict, caps: dict,
                exposure: float, buying_power: float, n_positions: int,
                day_pnl: float | None, log: list) -> dict:
    import rh_mcp
    lots = state["lots"]
    res = {"symbol": sym, "side": "buy", "reason": action.get("reason", ""), "status": "skipped"}
    quote = broker["quotes"].get(sym, {})
    dollar = action.get("dollar_amount")
    if dollar is None:
        res["reject_reason"] = "buy action without dollar_amount"
        return res
    canary = None
    if armed() and not state.get("live_round_trip_done"):
        canary = float(os.environ.get("LIVE_CANARY_USD", "20"))
    plan = size_entry(float(dollar), quote, caps, canary_usd=canary)
    if not plan.get("ok"):
        res["reject_reason"] = plan.get("reject_reason")
        return res
    existing_val = (broker["positions"].get(sym, {}).get("qty", 0.0)
                    * (quote.get("last") or quote.get("ask") or 0.0))
    ok, reason = check_entry_caps(plan, existing_val=existing_val, exposure=exposure,
                                  buying_power=buying_power,
                                  n_positions=n_positions, held=sym in broker["positions"],
                                  caps=caps, day_pnl=day_pnl)
    if not ok:
        res.update(reject_reason=reason, plan=plan)
        return res
    spec = buy_spec(sym, plan)
    res.update(plan=plan, order_spec=spec)

    # review (read-only) regardless of armed state — real broker alerts, no execution
    review = rh_mcp.review(spec)
    blocking = review_blocking(review)
    res["review_alerts"] = blocking
    if blocking:
        res.update(status="skipped", reject_reason=f"blocking review alert(s): {blocking}")
        log.append({"event": "buy_blocked", "symbol": sym, "spec": spec, "alerts": blocking})
        return res

    if not armed():
        res["status"] = "dryrun"
        log.append({"event": "buy_dryrun", "symbol": sym, "spec": spec, "plan": plan})
        return res

    ref_id = str(uuid.uuid4())
    placed = rh_mcp.place(spec, ref_id=ref_id)
    # A place can FAIL at the broker even when review was clean (e.g. an incomplete investor profile
    # 400s here, not in review). Only record a pending lot when an order id actually came back —
    # otherwise log it as failed so a rejected order never becomes a phantom position.
    order = order_obj(placed)
    order_id = _first(order, "id", "order_id") if isinstance(order, dict) else None
    if not order_id or (isinstance(placed, dict) and placed.get("errors")):
        res.update(status="failed", ref_id=ref_id, reject_reason=f"place rejected/failed: {placed}", order=placed)
        log.append({"event": "buy_failed", "symbol": sym, "spec": spec, "ref_id": ref_id, "result": placed})
        return res
    # record a pending lot; reconciliation next tick confirms the fill from broker positions and
    # arms the resting stop off the real cost basis.
    lots[sym] = {**lots.get(sym, {}), "entry_ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "scaled": lots.get(sym, {}).get("scaled", []), "stop_type": plan["stop_type"],
                 "resting_stop_order_id": None, "last_entry_ref_id": ref_id, "pending": True,
                 "entry_order_id": order_id}
    res.update(status="placed", ref_id=ref_id, order_id=order_id, order=placed)
    log.append({"event": "buy_placed", "symbol": sym, "spec": spec, "ref_id": ref_id,
                "order_id": order_id, "plan": plan, "order": placed})
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", required=True)
    ap.add_argument("--decision")
    ap.add_argument("--skip", action="store_true")
    args = ap.parse_args()

    context = load_json(Path(args.context))
    caps = context["caps"]
    now = datetime.now(timezone.utc)

    if str(context.get("mode", "paper")).lower() != "live":
        print("[live_execute] refusing: context mode is not 'live'", file=sys.stderr)
        return 2
    if not SNAPSHOT_PATH.exists():
        print("[live_execute] FATAL: no broker_snapshot.json — failing closed (no blind trading)", file=sys.stderr)
        return 2

    broker = parse_snapshot(load_json(SNAPSHOT_PATH))
    state = load_state()
    state["_caps"] = caps  # transient: lets reconcile() compute stop/TP off configured pcts

    # equity / day P&L from broker truth; persist start-of-day equity (broker doesn't track it).
    today = context.get("ts_et", "")[:10]
    exposure = sum((p["qty"] * (broker["quotes"].get(s, {}).get("last") or p.get("avg_cost") or 0.0))
                   for s, p in broker["positions"].items())
    equity = round(broker["buying_power"] + exposure, 2)
    if state.get("day") != today or state.get("start_of_day_equity") is None:
        state["day"] = today
        state["start_of_day_equity"] = equity
    day_pnl = round(equity - (state.get("start_of_day_equity") or equity), 2)

    log: list = []
    results: list = []
    reconcile(state, broker, log)

    is_dryrun = not armed()
    mode_tag = "live-dryrun" if is_dryrun else "live"

    if not args.skip and args.decision:
        decision = load_json(Path(args.decision))
        actions = decision.get("actions", [])
        # sells first (free up shares / honour exits), then buys
        for a in [x for x in actions if str(x.get("side")).lower() == "sell"]:
            results.append(execute_sell(str(a.get("symbol", "")).upper(), a, state, broker, caps, log))
        # re-check the breaker once before any entry (deterministic, on broker numbers)
        breaker = day_pnl <= -caps.get("DAILY_MAX_LOSS_USD", 150.0)
        # Running tallies so multiple buys in ONE tick respect the caps CUMULATIVELY: a placed buy
        # consumes buying power, adds exposure, and may open a new position slot. Without this each
        # buy checks against the pre-tick snapshot and N buys could collectively breach the caps.
        # Mirrors paper's per-fill recompute (apply_decision.validate_and_fill).
        run_exposure, run_bp, run_npos = exposure, broker["buying_power"], len(broker["positions"])
        for a in [x for x in actions if str(x.get("side")).lower() == "buy"]:
            sym = str(a.get("symbol", "")).upper()
            if breaker:
                results.append({"symbol": a.get("symbol"), "side": "buy", "status": "skipped",
                                "reject_reason": f"circuit_breaker day_pnl={day_pnl}"})
                continue
            if not context.get("allow_entries", False):
                results.append({"symbol": a.get("symbol"), "side": "buy", "status": "skipped",
                                "reject_reason": "entries disabled (market closed/stale)"})
                continue
            r = execute_buy(sym, a, state, broker, caps, run_exposure, run_bp, run_npos, day_pnl, log)
            results.append(r)
            if r.get("status") == "placed":
                notional = (r.get("plan") or {}).get("notional") or 0.0
                run_exposure += notional
                run_bp = max(0.0, run_bp - notional)
                if sym not in broker["positions"]:
                    run_npos += 1

    # NOTE: the canary round-trip flag (state["live_round_trip_done"]) is now set in reconcile() when
    # a lot we entered actually closes — NOT on a placed buy. The cap must survive until a real
    # entry->exit cycle completes, not lift the moment the first order fills.
    state.pop("_caps", None)
    write_json_atomic(STATE_PATH, state)

    record = {
        "ts_utc": now.isoformat(timespec="seconds"), "ts_et": context.get("ts_et"), "mode": mode_tag,
        "session": context.get("session"), "regime": context.get("regime", {}).get("posture"),
        "armed": armed(), "action": "skip" if (args.skip or not args.decision) else "decide",
        "buying_power": broker["buying_power"], "exposure": round(exposure, 2),
        "equity": equity, "day_pnl": day_pnl, "results": results, "reconcile": log,
        "n_placed": sum(1 for r in results if r.get("status") == "placed"),
        "n_skipped": sum(1 for r in results if r.get("status") in ("skipped", "dryrun")),
        "positions": broker["positions"],
    }
    if not args.skip and args.decision:
        d = load_json(Path(args.decision))
        if d.get("screen"):
            record["screen"] = d["screen"]
        if d.get("dd"):
            record["dd"] = d["dd"]
    ENGINE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ENGINE_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")

    # Mirror every PLACED live order to the unified trade history (data/trades.jsonl + daily
    # blotter), tagged with mode_tag (live / live-dryrun). Best-effort; never break a tick.
    trade_log.record_fills(results, ts_utc=record["ts_utc"], ts_et=record.get("ts_et"),
                           mode=mode_tag)

    placed = record["n_placed"]
    note = "DRY-RUN" if is_dryrun else "ARMED"
    print(f"[{record['ts_et']}] {mode_tag.upper()} {note} — {placed} placed, {record['n_skipped']} "
          f"skipped | equity={equity} day_pnl={day_pnl} bp={broker['buying_power']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
