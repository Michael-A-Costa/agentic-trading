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
  - BUY  -> ALWAYS whole shares: type=limit (marketable, capped MARKETABLE_LIMIT_PCT above the ask)
             + resting type=stop_market (GTC) armed at the broker once the fill confirms.
             If even 1 share exceeds MAX_POSITION_USD, the entry is skipped (not fractional).
  - SELL (exit/scale)-> cancel any resting stop first, then limit (whole) / market (fractional)

Safety gate (no human per-trade approval — this IS the seatbelt):
  - account hard-pinned to AGENTIC_ACCOUNT
  - every BUY: review_equity_order -> Python inspects alerts -> place only if clear
  - LIVE_ARMED!=1 => DRY-RUN: review + log "would place", never place
  - caps re-checked against fresh broker buying power; daily breaker on broker start-of-day equity
  - ref_id idempotency per logical order

NOTE: the exact JSON field names returned by the RH read tools are not known until a live dry-run;
the parse_* helpers try the likely keys and fail safe. Lock them down after the first dry-run.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
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


def gfv_guard_on() -> bool:
    """Cash-account settlement guard (default ON). Disable only for a margin account."""
    return str(os.environ.get("CASH_SETTLEMENT_GUARD", "1")).strip().lower() in ("1", "true", "yes", "on")


def next_settle_date(et_today: str) -> str:
    """T+1 BUSINESS day from an ET date 'YYYY-MM-DD' (US equities settle T+1 since 2024-05-28).
    Skips weekends; does NOT account for market holidays (a holiday makes the guard slightly less
    conservative — acceptable, and rare). Returns 'YYYY-MM-DD'."""
    try:
        d = datetime.strptime(et_today[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return et_today[:10]
    d += timedelta(days=1)
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d += timedelta(days=1)
    return d.isoformat()


def settled_buying_power(state: dict, broker: dict, et_today: str) -> tuple[float, float]:
    """Deployable (settled) cash = broker buying_power - unsettled SALE proceeds - pending deposits.

    GFV (Good-Faith Violation) on a CASH account happens when you BUY with unsettled funds and then
    sell before they settle. Sizing every entry against settled-only cash makes that impossible —
    necessary and sufficient. state['unsettled'] is our ledger of recent sale proceeds, each with a
    T+1 settle_date; prune the matured ones (in place) and subtract the rest. Returns (settled_bp,
    unsettled_total)."""
    bp = broker.get("buying_power") or 0.0
    if not gfv_guard_on():
        return bp, 0.0
    led = state.get("unsettled") or []
    state["unsettled"] = [u for u in led if str(u.get("settle_date", "")) > et_today]  # keep un-matured
    unsettled = sum(_f(u.get("amount"), 0.0) or 0.0 for u in state["unsettled"])
    pending = broker.get("pending_deposits", 0.0) or 0.0
    return max(0.0, bp - unsettled - pending), round(unsettled, 2)


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
    peeling the {"data": {...}} envelope. Returns None if the relay errored or shape is unusable.

    The relay is non-deterministic about nesting: it sometimes returns the raw tool response
    ({"order": {"data": {"order": {id, ...}}}}) and sometimes the extracted object ({"order": {id, ...}}).
    Handle both by peeling up to two layers."""
    if not isinstance(placed, dict):
        return None
    o = _unwrap(placed.get("order", placed))
    # relay may echo {"order": {...}} with one additional nesting level (raw MCP tool response)
    if isinstance(o, dict) and "id" not in o and "order_id" not in o and isinstance(o.get("order"), dict):
        o = o["order"]
    return o if isinstance(o, dict) else None


def _parse_quotes(raw_q) -> dict:
    """Parse a get_equity_quotes blob (data.results[].quote.{bid_price,ask_price,last_trade_price})
    into {SYM: {bid, ask, last}}. Shared by parse_snapshot and the live entry-quote backfill."""
    raw_q = _unwrap(raw_q or {})
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
    return quotes


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
    # Cash account: the broker exposes NO settled/unsettled split (confirmed 2026-06-08) — only cash,
    # buying_power, and pending_deposits. We exclude pending (un-cleared ACH) from deployable funds and
    # self-track unsettled SALE proceeds (see settled_buying_power) to avoid Good-Faith Violations.
    pending_deposits = _f(_first(pf, "pending_deposits"), 0.0) or 0.0
    # Total cash balance — the NAV/equity cash leg. DISTINCT from buying_power: in a cash account
    # buying_power excludes unsettled sale proceeds (and pending deposits), so buying_power < cash
    # whenever a recent sell hasn't settled. Equity/day-P&L must use the FULL cash (= broker total_value
    # cash leg); buying_power is only for what we can SPEND. Conflating them understated equity by the
    # unsettled amount and manufactured a phantom intraday loss. Falls back to buying_power if absent.
    cash = _f(_first(pf, "cash", "cash_balance"), None)
    if cash is None:
        cash = buying_power

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

    quotes = _parse_quotes(snap.get("quotes"))

    raw_o = _unwrap(snap.get("orders") or {})
    if isinstance(raw_o, dict):
        raw_o = raw_o.get("orders") or raw_o.get("results") or []
    orders = [o for o in (raw_o or []) if isinstance(o, dict)]
    return {"buying_power": buying_power, "cash": cash, "positions": positions, "quotes": quotes, "orders": orders,
            "pending_deposits": pending_deposits}


def open_stops_for(orders: list, sym: str) -> list:
    """All OPEN resting protective stops (sell) for a symbol. A Robinhood stop comes back with a
    non-null stop_price (type may read 'market'/'limit' with trigger='stop'), so we key on stop_price
    + side, NOT type=='stop_market'. Plural form is used to sweep duplicates after a trail re-arm."""
    OPEN = {"new", "queued", "confirmed", "unconfirmed", "partially_filled"}
    out = []
    for o in orders:
        osym = (_first(o, "symbol", "ticker") or "").upper().strip()
        oside = (_first(o, "side") or "").lower()
        ostate = (_first(o, "state", "status") or "").lower()
        has_stop = _first(o, "stop_price") is not None or "stop" in (_first(o, "type", "trigger", "order_type") or "").lower()
        if osym == sym and oside == "sell" and has_stop and ostate in OPEN:
            out.append(o)
    return out


def open_stop_for(orders: list, sym: str) -> dict | None:
    """The first OPEN resting protective stop (sell) for a symbol, or None (see open_stops_for)."""
    stops = open_stops_for(orders, sym)
    return stops[0] if stops else None


def open_nonstop_sells_for(orders: list, sym: str) -> list:
    """OPEN sell orders for a symbol that are NOT protective stops — i.e. a discretionary/exit limit
    that hasn't filled. A stranded one holds the shares (blocking a stop re-arm) AND leaves the lot
    naked to downside while it rests above the market; reconcile() sweeps these before re-arming."""
    OPEN = {"new", "queued", "confirmed", "unconfirmed", "partially_filled"}
    out = []
    for o in orders:
        osym = (_first(o, "symbol", "ticker") or "").upper().strip()
        oside = (_first(o, "side") or "").lower()
        ostate = (_first(o, "state", "status") or "").lower()
        has_stop = _first(o, "stop_price") is not None or "stop" in (_first(o, "type", "trigger", "order_type") or "").lower()
        if osym == sym and oside == "sell" and not has_stop and ostate in OPEN:
            out.append(o)
    return out


# ---------------------------------------------------------------------------
# PURE order-spec builders + cap checks  (unit-tested; no MCP, no I/O)
# ---------------------------------------------------------------------------
def size_entry(dollar_amount: float, quote: dict, caps: dict) -> dict:
    """Size a BUY as whole shares only (-> marketable limit + real resting broker stop).

    Floor to whole shares when the budget covers >=1. If the budget covers < 1 share, round UP
    to exactly 1 share when 1 share fits within the position cap — this overspends the DD's
    conviction sizing by at most 1 share but gets a real resting stop instead of a synthetic one.
    If even 1 share exceeds the cap, return ok=False: the entry is skipped, not degraded to
    fractional.
    """
    ref = quote.get("ask") or quote.get("last") or quote.get("bid")
    if not ref or ref <= 0:
        return {"ok": False, "reject_reason": "no usable quote (ask/last) for sizing"}
    notional = float(dollar_amount)
    limit_pct = caps.get("MARKETABLE_LIMIT_PCT", 0.5)
    raw_qty = notional / ref
    if math.floor(raw_qty) >= 1:
        qty = float(math.floor(raw_qty))
    else:
        # Budget is short of 1 share — round up to exactly 1 if it fits within the cap.
        ceiling = float(caps.get("MAX_POSITION_USD", 0) or 0)
        if ceiling > 0 and ref <= ceiling:
            qty = 1.0
        else:
            return {"ok": False,
                    "reject_reason": (f"whole-share-only: 1 share (${ref:.2f}) exceeds "
                                      f"cap (${ceiling:.2f}) — skipping rather than going fractional")}
    limit_price = round(ref * (1 + limit_pct / 100.0), 2)
    notional = qty * limit_price
    return {"ok": True, "kind": "limit", "qty": qty, "whole": True, "stop_type": "resting",
            "limit_price": limit_price, "notional": round(notional, 2)}


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


def trail_stop_price(lot: dict, caps: dict, last: float | None) -> tuple[float | None, float | None]:
    """Trailing-stop ratchet (PURE; no I/O, no MCP). Updates the high-water mark from `last` and
    returns (new_stop, high_water):

      - high_water = max(prior high-water, entry, last) — the peak the trail anchors to (caller persists).
      - new_stop   = a RATCHET-ONLY raise of lot['stop_price'], or None when no change is warranted.

    The stop SCHEDULE has two independent ratchet rungs (highest engaged rung wins, never below the
    entry-based STOP_LOSS_PCT floor, never lowered):
      - TRAIL_BREAKEVEN_AT_PCT: once the peak is up this %, lift the stop to entry ("no give-back to a
        loss"). One-time; can't whipsaw on the upside.
      - TRAIL_STOP_PCT (+ TRAIL_ACTIVATE_PCT): once up the activate %, ride TRAIL_STOP_PCT below the
        high-water mark, scaling up with every new high.
    Both rungs OFF (<=0) -> returns (None, high_water), the fixed entry-based stop untouched (today's
    behaviour). A raise smaller than TRAIL_MIN_STEP_PCT of the current stop is suppressed (churn guard —
    each whole-share re-arm costs a cancel+place + a brief naked window).
    """
    entry = _f(lot.get("entry_price"))
    if entry is None or entry <= 0:
        return None, lot.get("high_water")
    hw = _f(lot.get("high_water")) or entry
    if last is not None and last > hw:
        hw = last
    gain_pct = (hw / entry - 1.0) * 100.0
    trail = caps.get("TRAIL_STOP_PCT", 0.0) or 0.0
    be_at = caps.get("TRAIL_BREAKEVEN_AT_PCT", 0.0) or 0.0
    if trail <= 0 and be_at <= 0:
        return None, hw  # both rungs off — leave the fixed STOP_LOSS_PCT stop as-is

    # A stop SCHEDULE (ratchet-only), composed of independent rungs; take the highest that's engaged:
    candidates = []
    #  rung 1 — breakeven: once up TRAIL_BREAKEVEN_AT_PCT, lift the stop to entry ("no give-back to a
    #  loss"). One-time; cannot whipsaw on the upside (it sits far below price).
    if be_at > 0 and gain_pct >= be_at:
        candidates.append(entry)
    #  rung 2 — continuous trail: once up TRAIL_ACTIVATE_PCT, ride TRAIL_STOP_PCT below the high-water
    #  mark, scaling UP with every new high.
    if trail > 0 and gain_pct >= (caps.get("TRAIL_ACTIVATE_PCT", 0.0) or 0.0):
        candidates.append(hw * (1 - trail / 100.0))
    if not candidates:
        return None, hw  # neither rung engaged yet

    floor = round(entry * (1 - caps.get("STOP_LOSS_PCT", 8.0) / 100.0), 2)
    desired = max(round(max(candidates), 2), floor)  # never below the catastrophe stop
    cur = _f(lot.get("stop_price"))
    if cur is not None:
        if desired <= cur + 1e-9:
            return None, hw  # ratchet-only: never lower
        min_step = caps.get("TRAIL_MIN_STEP_PCT", 0.5) or 0.0
        if min_step > 0 and desired < cur * (1 + min_step / 100.0):
            return None, hw  # raise too small — skip the churn
    return desired, hw


def sell_spec(sym: str, qty: float, *, whole: bool, quote: dict, caps: dict, urgent: bool = False) -> dict:
    """Exit/discretionary sell.

    urgent=True — a protective full-close that REPLACES the resting stop_market (risk-exit, synthetic-
    stop/TP hit, EOD flatten, max-hold, manage 'exit'). Use a plain MARKET sell: same execution
    semantics as the stop it stands in for, and — unlike a limit — it CANNOT land non-marketable and
    rest above a fast-dropping market while the lot sits naked (the bug that stranded a risk-exit limit
    on ALOY after its protective stop had already been cancelled).

    urgent=False — a partial scale-out / trim. The remaining lot keeps its protection, so favour price:
    marketable limit for a whole-share slice, market for a fractional one."""
    if not urgent and whole and float(qty) == math.floor(float(qty)):
        ref = quote.get("bid") or quote.get("last") or quote.get("ask")
        limit_pct = caps.get("MARKETABLE_LIMIT_PCT", 0.5)
        if ref and ref > 0:
            lp = round(ref * (1 - limit_pct / 100.0), 2)
            return {"symbol": sym, "side": "sell", "type": "limit", "quantity": str(int(qty)),
                    "limit_price": f"{lp:.2f}", "time_in_force": "gfd", "market_hours": "regular_hours"}
    # urgent exit, fractional, or no quote to anchor a limit -> market sell (fractional is market-only anyway)
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
    return {"lots": {}, "day": None, "start_of_day_equity": None}


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
def _retrail_resting(sym: str, qty: int, new_stop: float, lot: dict, orders: list, log: list) -> None:
    """Move a whole-share lot's resting stop UP (ratchet): cancel the current stop, place a higher one.
    The MCP exposes no modify, so cancel+place is the only path. Failure handling keeps the lot from
    ever being left unprotected:
      - cancel fails  -> the old stop is still resting (safe); don't place a duplicate, retry next tick.
      - place  fails  -> lot is momentarily bare; degrade to a synthetic stop at new_stop (engine-tick
                          cover) and clear the dead id so the next reconcile re-arms a real resting stop.
    After a successful re-arm, sweep any OTHER open sell-stop for the symbol (a prior silently-failed
    cancel would otherwise leave two stops that double-fire on trigger)."""
    import rh_mcp
    old_id = lot.get("resting_stop_order_id")
    old_stop = lot.get("stop_price")
    if old_id:
        c = rh_mcp.cancel(old_id)
        if isinstance(c, dict) and c.get("errors"):
            log.append({"event": "trail_cancel_failed", "symbol": sym, "order_id": old_id,
                        "result": c, "kept_stop": old_stop})
            return  # old stop still live -> protected; don't stack a second stop
    ref_id = str(uuid.uuid4())
    res = rh_mcp.place(stop_spec(sym, qty, new_stop), ref_id=ref_id)
    o = order_obj(res)
    new_id = _first(o, "id", "order_id") if isinstance(o, dict) else None
    if not new_id or (isinstance(res, dict) and res.get("errors")):
        lot["resting_stop_order_id"] = None
        lot["stop_type"] = "synthetic"
        lot["stop_price"] = new_stop
        log.append({"event": "trail_rearm_failed", "symbol": sym, "from": old_stop, "to": new_stop,
                    "ref_id": ref_id, "result": res, "fallback": "synthetic"})
        return
    lot["resting_stop_order_id"] = new_id
    lot["stop_type"] = "resting"
    lot["stop_price"] = new_stop
    log.append({"event": "trail_rearm", "symbol": sym, "from": old_stop, "to": new_stop,
                "order_id": new_id, "ref_id": ref_id})
    for extra in open_stops_for(orders, sym):  # duplicate sweep (orders = pre-place snapshot)
        eid = _first(extra, "id", "order_id")
        if eid and eid not in (new_id, old_id):
            rh_mcp.cancel(eid)
            log.append({"event": "trail_dup_cancel", "symbol": sym, "order_id": eid})


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
            base_stop = round(lot["entry_price"] * (1 - sl / 100.0), 4)
            # ratchet-safe: a trailing stop may have raised stop_price above the initial level on a
            # prior tick — never reset it back down to the entry-based floor here.
            prev = _f(lot.get("stop_price"))
            lot["stop_price"] = max(base_stop, prev) if prev is not None else base_stop
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
                # First clear any stranded prior-tick exit limit: it holds the shares (so the stop-arm
                # below would reject) AND leaves the lot naked while it rests above the market. Safe to
                # cancel here — reconcile runs before this tick's sells, so an open non-stop sell can
                # only be a leftover. If the engine still wants out, this tick re-decides and fires a
                # fill-certain market exit. (Auto-recovery for the strand that left ALOY unprotected.)
                for so in open_nonstop_sells_for(orders, sym):
                    sid = _first(so, "id", "order_id")
                    if sid:
                        rh_mcp.cancel(sid)
                        log.append({"event": "stranded_sell_cancelled", "symbol": sym, "order_id": sid})
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

        # --- trailing stop: ratchet the protective stop UP toward the high-water mark, never down.
        # OFF unless TRAIL_STOP_PCT>0. Whole-share lots move the resting broker stop (cancel+replace);
        # fractional/synthetic lots ratchet stop_price in place (engine-tick cover, no broker order).
        q = broker["quotes"].get(sym) or {}
        new_stop, hw = trail_stop_price(lot, state.get("_caps", {}), _f(_first(q, "last", "bid")))
        if hw is not None:
            lot["high_water"] = hw
        if new_stop is not None and new_stop > (_f(lot.get("stop_price")) or 0.0):
            old_stop = lot.get("stop_price")
            if whole and lot.get("resting_stop_order_id") and lot.get("stop_type") == "resting":
                if do_arm:
                    _retrail_resting(sym, math.floor(bp["qty"]), new_stop, lot, orders, log)
                else:
                    lot["stop_price"] = new_stop  # dry-run: reflect intended level (stop is synthetic)
                    log.append({"event": "trail_rearm_dryrun", "symbol": sym,
                                "from": old_stop, "to": new_stop, "high_water": hw})
            else:
                lot["stop_price"] = new_stop  # synthetic / fractional: pure-data ratchet, no MCP
                log.append({"event": "trail_synthetic", "symbol": sym,
                            "from": old_stop, "to": new_stop, "high_water": hw})

    # 2) lots we track but the broker no longer holds. Two cases:
    #    a) a PENDING entry from a prior tick that never showed as a position -> it didn't fill
    #       (marketable limit gapped away / GFD expired). Cancel any still-open entry order and drop
    #       the lot — do NOT book it as a closed position (it was never opened), and do NOT count it
    #       as a round-trip.
    #    b) a position we actually held that's now gone -> a real closure (stop fired / sold while
    #       asleep). Record the exit.
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
    # A FULL close (no explicit qty) is a protective/decisive exit that gives up the resting stop ->
    # fill-certain MARKET sell so it can't strand as a resting limit while the lot sits naked. A partial
    # (scale-out / trim, qty set) leaves the lot protected, so it keeps the price-protected limit.
    urgent = action.get("qty") is None
    spec = sell_spec(sym, qty, whole=whole, quote=broker["quotes"].get(sym, {}), caps=caps, urgent=urgent)
    res.update(order_spec=spec, qty=qty, urgent=urgent)

    if not armed():
        res["status"] = "dryrun"
        log.append({"event": "sell_dryrun", "symbol": sym, "spec": spec, "urgent": urgent,
                    "cancel_stop": lot.get("resting_stop_order_id")})
        return res
    # cancel resting stop first so the shares are free to sell. Remember it: if the sell place FAILS we
    # must RE-ARM, never leave a whole-share lot naked because we dropped the stop for a sell that died.
    had_stop_id = lot.get("resting_stop_order_id")
    had_stop_price = _f(lot.get("stop_price"))
    if had_stop_id:
        c = rh_mcp.cancel(had_stop_id)
        log.append({"event": "cancel_stop", "symbol": sym, "order_id": had_stop_id, "result": c})
        lot["resting_stop_order_id"] = None
    ref_id = str(uuid.uuid4())
    placed = rh_mcp.place(spec, ref_id=ref_id)
    o = order_obj(placed)
    order_id = _first(o, "id", "order_id") if isinstance(o, dict) else None
    if not order_id or (isinstance(placed, dict) and placed.get("errors")):
        res.update(status="failed", ref_id=ref_id, reject_reason=f"sell rejected/failed: {placed}", order=placed)
        log.append({"event": "sell_failed", "symbol": sym, "spec": spec, "ref_id": ref_id, "result": placed})
        # the sell didn't take -> re-arm the protective stop we just cancelled (whole-share lots only;
        # fractional lots never had a resting stop). Leaves the lot protected for the next tick.
        if had_stop_id and whole and had_stop_price:
            rid = str(uuid.uuid4())
            rearm = rh_mcp.place(stop_spec(sym, math.floor(qty), had_stop_price), ref_id=rid)
            ro = order_obj(rearm)
            roid = _first(ro, "id", "order_id") if isinstance(ro, dict) else None
            lot["resting_stop_order_id"] = roid
            lot["stop_type"] = "resting" if roid else "synthetic"
            res["stop_rearmed"] = bool(roid)
            log.append({"event": "stop_rearmed_after_failed_sell" if roid else "stop_rearm_failed",
                        "symbol": sym, "stop_price": had_stop_price, "ref_id": rid,
                        "order_id": roid, "result": None if roid else rearm})
        return res
    res.update(status="placed", ref_id=ref_id, order_id=order_id, order=placed)
    log.append({"event": "sell_placed", "symbol": sym, "spec": spec, "ref_id": ref_id,
                "order_id": order_id, "order": placed})
    return res


def _confirm_recent_buy(sym: str, log: list) -> dict | None:
    """Re-read broker orders to confirm a place whose relay echo was unparseable. Returns the newest
    agentic BUY for sym created in the last ~2.5 min that isn't cancelled/rejected — that's our order
    (we place at most one buy per symbol per tick). None if nothing matches (a genuine no-op)."""
    import rh_mcp
    since = (datetime.now(timezone.utc) - timedelta(seconds=150)).isoformat(timespec="seconds")
    blob = rh_mcp.recent_orders(sym, created_at_gte=since)
    raw = _unwrap((blob or {}).get("orders") or {})
    if isinstance(raw, dict):
        raw = raw.get("orders") or raw.get("results") or []
    best = None
    for o in raw or []:
        if not isinstance(o, dict) or str(o.get("side", "")).lower() != "buy":
            continue
        if str(o.get("state", "")).lower() in ("cancelled", "rejected", "failed", "voided"):
            continue
        if best is None or str(o.get("created_at", "")) > str(best.get("created_at", "")):
            best = o
    if best:
        log.append({"event": "place_confirmed_via_reread", "symbol": sym,
                    "order_id": _first(best, "id", "order_id"), "state": best.get("state")})
    return best


def _read_order(sym: str, order_id: str) -> dict | None:
    """Re-read ONE just-placed order from broker truth to get its CURRENT fill state (state /
    cumulative_quantity / average_price). The place echo is captured at submission, so a marketable
    limit reads there as unfilled — we re-read to see the fill and size the stop off the real average
    price. Returns the matching order dict, or None if it isn't visible yet."""
    import rh_mcp
    since = (datetime.now(timezone.utc) - timedelta(seconds=180)).isoformat(timespec="seconds")
    blob = rh_mcp.recent_orders(sym, created_at_gte=since)
    raw = _unwrap((blob or {}).get("orders") or {})
    if isinstance(raw, dict):
        raw = raw.get("orders") or raw.get("results") or []
    for o in raw or []:
        if isinstance(o, dict) and _first(o, "id", "order_id") == order_id:
            return o
    return None


def _arm_entry_stop(sym: str, plan: dict, order: dict | None, order_id: str,
                    caps: dict, state: dict, log: list) -> None:
    """Force a protective stop IN THE SAME TICK as the entry, instead of leaving the lot naked until
    the next reconcile ~10 min later. A whole-share BUY is a marketable limit that fills within
    seconds; once it's confirmed filled we arm the resting stop_market right away.

    Flow: read the fill (from the place echo, else re-read the order from broker truth) -> if >=1 whole
    share has filled, size the stop off the REAL average fill price and place a resting stop_market.
    On a stop-place failure, leave the lot with a synthetic stop level + qty set so the 1-min sentinel
    covers it AND next tick's reconcile re-arms a real resting stop. If the buy isn't confirmed filled
    in-tick (rare for a marketable limit), the lot stays 'pending' and reconcile arms it next tick."""
    import rh_mcp
    lot = state["lots"][sym]
    state_str = str(_first(order or {}, "state", "status") or "").lower()
    filled_qty = _f(_first(order or {}, "cumulative_quantity", "filled_quantity"), 0.0) or 0.0
    avg = _f(_first(order or {}, "average_price", "average_buy_price"))
    if state_str != "filled" or filled_qty < 1:  # place echo is pre-fill -> re-read broker truth
        fresh = _read_order(sym, order_id)
        if fresh:
            filled_qty = _f(_first(fresh, "cumulative_quantity", "filled_quantity"), 0.0) or 0.0
            avg = _f(_first(fresh, "average_price", "average_buy_price")) or avg
    whole = math.floor(filled_qty)
    if whole < 1:
        return  # not filled yet in-tick -> lot stays pending; reconcile + sentinel are the backstop
    entry = avg or plan.get("limit_price")
    if not entry or entry <= 0:
        return  # no price to size a stop off -> leave pending for reconcile
    sl = caps.get("STOP_LOSS_PCT", 8.0)
    tp = caps.get("TAKE_PROFIT_PCT", 12.0)
    stop_price = round(entry * (1 - sl / 100.0), 2)
    # Fill confirmed in-tick -> the lot is real (no longer pending). Populate the synthetic levels too
    # so the sentinel can watch it in any window where the resting stop isn't (yet) armed.
    lot.update(qty=float(whole), entry_price=entry, pending=False, high_water=entry,
               stop_price=stop_price, take_profit_price=round(entry * (1 + tp / 100.0), 4))
    ref_id = str(uuid.uuid4())
    res = rh_mcp.place(stop_spec(sym, whole, stop_price), ref_id=ref_id)
    o = order_obj(res)
    sid = _first(o, "id", "order_id") if isinstance(o, dict) else None
    lot["resting_stop_order_id"] = sid
    lot["stop_type"] = "resting" if sid else "synthetic"
    log.append({"event": "arm_stop_on_entry" if sid else "arm_stop_on_entry_failed", "symbol": sym,
                "stop_price": stop_price, "qty": whole, "order_id": sid, "ref_id": ref_id,
                "result": None if sid else res})


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
    plan = size_entry(float(dollar), quote, caps)
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
    # The place relay is NON-DETERMINISTIC: it can place the order at the broker yet return None or
    # unparseable prose (a headless agent ignoring "no fences/prose", or a non-zero exit). Trusting
    # the echo turned real fills into "place rejected/failed" AND left resting orders untracked. So
    # when the echo lacks an id but didn't explicitly error, CONFIRM from broker truth before
    # declaring failure (truth is always re-read from the broker — the agent's prose is never trusted).
    if not order_id and not (isinstance(placed, dict) and placed.get("errors")):
        confirmed = _confirm_recent_buy(sym, log)
        if confirmed:
            order = confirmed
            order_id = _first(confirmed, "id", "order_id")
            placed = {"order": confirmed, "confirmed_via": "broker_reread"}
    if not order_id or (isinstance(placed, dict) and placed.get("errors")):
        res.update(status="failed", ref_id=ref_id, reject_reason=f"place rejected/failed: {placed}", order=placed)
        log.append({"event": "buy_failed", "symbol": sym, "spec": spec, "ref_id": ref_id, "result": placed})
        return res
    # record a pending lot; _arm_entry_stop below confirms the fill IN-TICK and arms the resting stop
    # immediately. If the fill isn't confirmed in-tick, the lot stays pending and reconcile arms it
    # next tick off the real cost basis.
    lots[sym] = {**lots.get(sym, {}), "entry_ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "scaled": lots.get(sym, {}).get("scaled", []), "stop_type": plan["stop_type"],
                 "resting_stop_order_id": None, "last_entry_ref_id": ref_id, "pending": True,
                 "entry_order_id": order_id}
    res.update(status="placed", ref_id=ref_id, order_id=order_id, order=placed)
    log.append({"event": "buy_placed", "symbol": sym, "spec": spec, "ref_id": ref_id,
                "order_id": order_id, "plan": plan, "order": placed})
    # Force a protective stop on this entry IN THIS TICK (confirm fill -> arm resting stop_market), so
    # the lot is never left naked for the ~10 min until the next reconcile. Backstop unchanged:
    # reconcile re-arms on the next tick if the fill wasn't confirmed here.
    _arm_entry_stop(sym, plan, order, order_id, caps, state, log)
    res["stop_armed"] = bool(lots[sym].get("resting_stop_order_id"))
    res["stop_type"] = lots[sym].get("stop_type")
    res["pending"] = bool(lots[sym].get("pending"))
    return res


def _parallel_orders_on() -> bool:
    """Whether to run this tick's order relays (sells AND entries) concurrently. Defaults ON.
    LIVE_PARALLEL_ORDERS is the master knob; falls back to the older LIVE_PARALLEL_ENTRIES so an
    existing opt-out still serializes everything."""
    v = os.environ.get("LIVE_PARALLEL_ORDERS", os.environ.get("LIVE_PARALLEL_ENTRIES", "1"))
    return str(v).strip().lower() not in ("0", "false", "no", "")


def _order_workers() -> int:
    """Max concurrent order-relay subprocesses. LIVE_ORDER_WORKERS, falling back to LIVE_ENTRY_WORKERS."""
    v = os.environ.get("LIVE_ORDER_WORKERS", os.environ.get("LIVE_ENTRY_WORKERS", "5"))
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return 5


def _run_relays_parallel(jobs: list, side: str) -> list:
    """Run independent order-relay thunks concurrently and collect their result dicts.

    Each `claude` relay is a ~30-50s I/O-bound subprocess; serializing N of them adds their runtimes
    (a 5-order phase = minutes of dead wall-clock). A thread pool overlaps the waits — total time
    ~= the slowest single relay. Tokens are UNAFFECTED: each relay is its own subprocess with its own
    prompt, so N calls cost the same whether serial or parallel (parallel may even cache marginally
    better). `jobs` is [(symbol, thunk)]; a thunk that raises becomes a 'failed' result rather than
    sinking the batch. Falls back to serial for <2 jobs or when parallelism is switched off."""
    results: list = []
    workers = min(len(jobs), _order_workers()) if _parallel_orders_on() else 1
    if workers > 1 and len(jobs) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(thunk): sym for sym, thunk in jobs}
            for fut in concurrent.futures.as_completed(futs):
                try:
                    results.append(fut.result())
                except Exception as e:  # noqa: BLE001 — one bad relay must not sink the rest
                    results.append({"symbol": futs[fut], "side": side, "status": "failed",
                                    "reject_reason": f"{side}_exception: {e}"})
    else:
        for sym, thunk in jobs:
            try:
                results.append(thunk())
            except Exception as e:  # noqa: BLE001
                results.append({"symbol": sym, "side": side, "status": "failed",
                                "reject_reason": f"{side}_exception: {e}"})
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", required=True)
    ap.add_argument("--decision")
    ap.add_argument("--skip", action="store_true")
    args = ap.parse_args()

    context = load_json(Path(args.context))
    caps = context["caps"]
    now = datetime.now(timezone.utc)

    # Relay token accounting: every rh_mcp call funnels through decide.run_claude, which appends to
    # decide's in-process usage ledger. Reset it now so usage_summary() at the end is THIS tick's
    # broker-I/O spend (snapshot runs in a separate process; this captures quotes/review/place/cancel).
    from decide import reset_usage, usage_summary
    reset_usage()

    if str(context.get("mode", "paper")).lower() != "live":
        print("[live_execute] refusing: context mode is not 'live'", file=sys.stderr)
        return 2
    if not SNAPSHOT_PATH.exists():
        print("[live_execute] FATAL: no broker_snapshot.json — failing closed (no blind trading)", file=sys.stderr)
        return 2

    broker = parse_snapshot(load_json(SNAPSHOT_PATH))
    state = load_state()
    state["_caps"] = caps  # transient: lets reconcile() compute stop/TP off configured pcts

    # Mark held positions from the CONTEXT. tick_context already fetched fresh public (Cboe) quotes for
    # every holding, so the broker snapshot no longer needs to quote them (it used to quote the pins —
    # the WRONG symbols — leaving 16/19 holdings marked at cost, which corrupted equity, day-P&L, and
    # the daily-loss breaker). Merge the context marks into broker["quotes"] so exposure, sell specs,
    # sell-proceeds, and the trailing stop all use live marks. Entry symbols still come from the backfill.
    for p in context.get("positions", []):
        s = str(p.get("symbol", "")).upper()
        last = p.get("last")
        if s and last and not (broker["quotes"].get(s) or {}).get("last"):
            broker["quotes"][s] = {"last": float(last), "bid": float(last), "ask": float(last)}

    # equity / day P&L from broker truth; persist start-of-day equity (broker doesn't track it).
    today = context.get("ts_et", "")[:10]
    exposure = sum((p["qty"] * (broker["quotes"].get(s, {}).get("last") or p.get("avg_cost") or 0.0))
                   for s, p in broker["positions"].items())
    # Equity = FULL cash + marked exposure (≈ broker total_value). Use cash, NOT buying_power: in a
    # cash account buying_power excludes unsettled sale proceeds, so using it here understated equity
    # by the unsettled amount and showed a phantom intraday loss. SOD-equity below uses this same
    # formula, so day_pnl stays internally consistent — it just no longer drifts with settlement timing.
    equity = round(broker["cash"] + exposure, 2)
    if state.get("day") != today or state.get("start_of_day_equity") is None:
        state["day"] = today
        state["start_of_day_equity"] = equity
    day_pnl = round(equity - (state.get("start_of_day_equity") or equity), 2)

    log: list = []
    results: list = []
    reconcile(state, broker, log)

    is_dryrun = not armed()
    mode_tag = "live-dryrun" if is_dryrun else "live"

    # Cash-account settlement guard: deployable cash = broker buying_power - unsettled sale proceeds -
    # pending deposits. Computed BEFORE this tick's sells append to the ledger (this tick's proceeds
    # are unsettled until T+1, so not deployable now). Also prunes matured entries from the ledger.
    settled_bp, unsettled_total = settled_buying_power(state, broker, today)

    if not args.skip and args.decision:
        decision = load_json(Path(args.decision))
        actions = decision.get("actions", [])
        # sells first (free up shares / honour exits), then buys. Exits are independent I/O-bound
        # relays (cancel resting stop + place sell), so run them CONCURRENTLY — same as entries below.
        # Serial exits added each relay's ~30-50s end-to-end; parallel collapses the phase to roughly
        # one relay. No token cost: each relay is its own subprocess, so N exits cost the same either
        # way — only wall-clock shrinks.
        sell_actions = [x for x in actions if str(x.get("side")).lower() == "sell"]
        if sell_actions:
            sell_jobs = [(str(a.get("symbol", "")).upper(),
                          (lambda s=str(a.get("symbol", "")).upper(), act=a:
                           execute_sell(s, act, state, broker, caps, log)))
                         for a in sell_actions]
            for r in _run_relays_parallel(sell_jobs, side="sell"):
                results.append(r)
                # Record sale proceeds as UNSETTLED (T+1) so a later tick can't redeploy them into a
                # buy and trip a Good-Faith Violation. Done HERE (post-relay, single-threaded) so the
                # unsettled ledger is mutated without races. Proceeds estimated at the sell-ref price.
                if r.get("status") == "placed" and gfv_guard_on():
                    qd = broker["quotes"].get(r["symbol"], {})
                    px = qd.get("bid") or qd.get("last") or qd.get("ask") or 0.0
                    proceeds = round((_f(r.get("qty"), 0.0) or 0.0) * px, 2)
                    if proceeds > 0:
                        state.setdefault("unsettled", []).append(
                            {"settle_date": next_settle_date(today), "amount": proceeds,
                             "symbol": r["symbol"], "sold_ts": now.isoformat(timespec="seconds")})
        # Backfill quotes for entry candidates BEFORE sizing. broker_snapshot only quotes the
        # CANDIDATES pins + indexes (it runs before decide, so it can't know the day's movers), so
        # size_entry would reject every discovery name with "no usable quote". Pull any missing buy
        # symbols live now in one batched relay call and merge into broker["quotes"].
        buy_syms = [str(x.get("symbol", "")).upper() for x in actions if str(x.get("side")).lower() == "buy"]
        missing = sorted({s for s in buy_syms if s and not (broker["quotes"].get(s) or {}).get("ask")
                          and not (broker["quotes"].get(s) or {}).get("last")})
        if missing:
            import rh_mcp
            qsnap = rh_mcp.quotes(missing)
            fetched = _parse_quotes((qsnap or {}).get("quotes")) if qsnap else {}
            broker["quotes"].update(fetched)
            log.append({"event": "entry_quotes_fetched", "requested": missing,
                        "got": sorted(fetched.keys())})

        # re-check the breaker once before any entry (deterministic, on broker numbers)
        breaker = day_pnl <= -caps.get("DAILY_MAX_LOSS_USD", 150.0)
        # Running tallies so multiple buys in ONE tick respect the caps CUMULATIVELY: a placed buy
        # consumes buying power, adds exposure, and may open a new position slot. Without this each
        # buy checks against the pre-tick snapshot and N buys could collectively breach the caps.
        # Mirrors paper's per-fill recompute (apply_decision.validate_and_fill). Entries draw on
        # SETTLED cash (the GFV guard), not raw broker buying_power.
        # Gather buy candidates that pass the cheap, deterministic filters (breaker, market-open,
        # armed-trigger). The expensive part — each entry's review+place relay (~30-50s cold-start
        # spawn) — runs AFTER, in PARALLEL: the relays are independent and I/O-bound, so serial blew
        # ticks to 800s+. Bounding the COUNT by headroom keeps the concurrent buys cap-safe.
        max_entries = int(os.environ.get("MAX_ENTRIES_PER_TICK", "5"))
        ready: list = []
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
            # Level-armed entry: the LLM wants to enter on a price trigger, not now. In PAPER the
            # sentinel fires these on the cross; in LIVE the sentinel is a no-op, so evaluate the
            # trigger here against the fresh quote and only enter once it's satisfied — otherwise skip
            # (re-checked next planner tick from the cached commit).
            trig = a.get("entry_trigger") if a.get("arm") else None
            if isinstance(trig, dict) and trig.get("price"):
                q = broker["quotes"].get(sym, {})
                px = q.get("last") or q.get("ask") or q.get("bid")
                tprice = _f(trig.get("price"))
                direction = str(trig.get("direction", "")).lower()
                if px is None or tprice is None:
                    results.append({"symbol": sym, "side": "buy", "status": "skipped",
                                    "reject_reason": "armed entry: no quote/trigger to evaluate"})
                    continue
                crossed = (px <= tprice) if direction == "below" else (px >= tprice) if direction == "above" else False
                if not crossed:
                    results.append({"symbol": sym, "side": "buy", "status": "skipped",
                                    "reject_reason": f"armed entry waiting: px {px} not {direction} {tprice}"})
                    continue
            ready.append((sym, a))

        # Bound the entry COUNT by available headroom so the PARALLEL entries can't collectively breach
        # the caps (each runs against the same pre-tick snapshot, blind to the others' fills). Even if
        # every slot fills at MAX_POSITION_USD ceiling; the total stays within settled cash AND the
        # exposure cap. execute_buy still re-checks each entry against the snapshot (belt+suspenders).
        ceil = float(caps.get("MAX_POSITION_USD", 0) or 0) or 1.0
        exp_headroom = max(0.0, float(caps.get("MAX_TOTAL_EXPOSURE_USD", 0.0)) - exposure)
        headroom_slots = int(min(settled_bp, exp_headroom) // ceil)
        slots = max(0, min(max_entries, headroom_slots, len(ready)))
        for sym, a in ready[slots:]:
            results.append({"symbol": sym, "side": "buy", "status": "skipped",
                            "reject_reason": f"deferred — slots used (max_entries={max_entries}, headroom_slots={headroom_slots})"})

        # Run the slot entries' review+place relays in PARALLEL (independent I/O-bound cold-start
        # spawns) — same machinery and knob as the exits above (LIVE_PARALLEL_ORDERS / _WORKERS).
        to_run = ready[:slots]
        if to_run:
            npos = len(broker["positions"])
            buy_jobs = [(s, (lambda s=s, act=act:
                            execute_buy(s, act, state, broker, caps, exposure, settled_bp, npos, day_pnl, log)))
                        for s, act in to_run]
            results.extend(_run_relays_parallel(buy_jobs, side="buy"))

    state.pop("_caps", None)
    write_json_atomic(STATE_PATH, state)

    record = {
        "ts_utc": now.isoformat(timespec="seconds"), "ts_et": context.get("ts_et"), "mode": mode_tag,
        "session": context.get("session"), "regime": context.get("regime", {}).get("posture"),
        "armed": armed(), "action": "skip" if (args.skip or not args.decision) else "decide",
        "buying_power": broker["buying_power"], "settled_buying_power": round(settled_bp, 2),
        "unsettled": round(unsettled_total, 2), "pending_deposits": broker.get("pending_deposits", 0.0),
        "exposure": round(exposure, 2),
        "equity": equity, "day_pnl": day_pnl, "results": results, "reconcile": log,
        "n_placed": sum(1 for r in results if r.get("status") == "placed"),
        "n_skipped": sum(1 for r in results if r.get("status") in ("skipped", "dryrun")),
        "positions": broker["positions"],
        "relay_token_usage": usage_summary(),  # broker-I/O spend this tick (quotes/review/place/cancel)
    }
    dd_usage: dict = {}
    if not args.skip and args.decision:
        d = load_json(Path(args.decision))
        if d.get("screen"):
            record["screen"] = d["screen"]
        if d.get("dd"):
            record["dd"] = d["dd"]
        dd_usage = d.get("token_usage") or {}  # Stage-2 DD/manage spend, folded into the tick total below
    ENGINE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ENGINE_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")

    # Mirror every PLACED live order to the unified trade history (data/trades.jsonl + daily
    # blotter), tagged with mode_tag (live / live-dryrun). Best-effort; never break a tick.
    trade_log.record_fills(results, ts_utc=record["ts_utc"], ts_et=record.get("ts_et"),
                           mode=mode_tag)

    placed = record["n_placed"]
    note = "DRY-RUN" if is_dryrun else "ARMED"
    gfv = f" settled_bp={round(settled_bp, 2)}" + (f" (unsettled={unsettled_total})" if unsettled_total else "")
    print(f"[{record['ts_et']}] {mode_tag.upper()} {note} — {placed} placed, {record['n_skipped']} "
          f"skipped | equity={equity} day_pnl={day_pnl} bp={broker['buying_power']}{gfv}")
    rtu = record["relay_token_usage"]
    if rtu.get("n_calls"):
        print(f"RELAY-TOKENS: {rtu['n_calls']} call(s), {rtu['total_tokens']:,} tok "
              f"(out {rtu['output_tokens']:,}) ~${rtu['cost_usd']:.4f}  [model={os.environ.get('RH_RELAY_MODEL', os.environ.get('RH_PLACE_MODEL', 'claude-haiku-4-5-20251001'))}]")
    # Grand-total LLM spend for the whole tick: Stage-2 DD/manage (decide.py) + broker relay (here).
    dd_cost, dd_calls = float(dd_usage.get("cost_usd") or 0.0), int(dd_usage.get("n_calls") or 0)
    relay_cost, relay_calls = float(rtu.get("cost_usd") or 0.0), int(rtu.get("n_calls") or 0)
    if dd_calls or relay_calls:
        print(f"TICK COST: ${dd_cost + relay_cost:.4f} over {dd_calls + relay_calls} call(s) "
              f"(dd ${dd_cost:.4f} · relay ${relay_cost:.4f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
