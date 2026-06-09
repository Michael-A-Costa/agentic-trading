#!/usr/bin/env python3
"""
live_snapshot.py — the ONE parser for the broker snapshot's portfolio + positions blobs.

Both the live executor (live_execute.parse_snapshot) and the live gate
(live_tick_context.load_live_state) read data/tick/broker_snapshot.json. They used to parse the
portfolio cash leg independently and DRIFTED: the executor was fixed to derive equity from the
broker's FULL cash while the gate still used buying_power, which understated equity by the unsettled
amount and tripped the daily-loss circuit breaker on a phantom intraday loss. This module is the
shared source of truth so that split can't happen again — both consumers parse cash/positions here.

Field shapes confirmed against live MCP output (2026-06-04 / 2026-06-08):
  get_portfolio        -> data.buying_power.buying_power (nested) / data.cash / data.pending_deposits
  get_equity_positions -> data.positions[].{quantity, average_buy_price, shares_available_for_sells}
Tool results may arrive wrapped as {"data": {...}, "guide": "..."}; _unwrap peels that envelope.
"""
from __future__ import annotations


def _f(x, default=None):
    """Coerce a possibly-string broker number to float; None/'' -> default."""
    if x is None or x == "":
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _first(d, *keys, default=None):
    """Return the first present, non-None key from a dict (defensive broker-field mapping)."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _unwrap(x):
    """Tool results come wrapped as {"data": {...}, "guide": "..."}. Peel the data envelope if present;
    pass through if the inner object was already handed to us."""
    return x["data"] if isinstance(x, dict) and isinstance(x.get("data"), dict) else x


def parse_portfolio(snap: dict) -> dict:
    """Parse the portfolio blob into {buying_power, cash, pending_deposits}.

    The cash/buying_power DISTINCTION is the whole reason this is shared:
      - buying_power = what we can SPEND now. On a cash account this EXCLUDES unsettled sale proceeds
        (and pending deposits), so buying_power < cash whenever a recent sell hasn't settled (T+1).
        Use it only for sizing/affordability — enforced downstream by the settled-cash GFV guard.
      - cash         = the FULL cash balance (settled + unsettled) — the NAV/equity cash leg. Equity
        and day-P&L MUST use this; conflating it with buying_power understates equity by the unsettled
        amount and manufactures a phantom intraday loss (which once tripped the daily-loss breaker).
    cash falls back to buying_power when the broker omits the cash field.
    """
    pf = _unwrap(snap.get("portfolio") or {})
    bp = pf.get("buying_power") if isinstance(pf, dict) else None
    if isinstance(bp, dict):  # nested {"buying_power": "1064.0000", "unleveraged_buying_power": ...}
        buying_power = _f(_first(bp, "buying_power", "unleveraged_buying_power"), None)
    else:
        buying_power = _f(bp, None)
    if buying_power is None:
        buying_power = _f(_first(pf, "cash", "buying_power_usd", "cash_available_for_trading"), 0.0)
    cash = _f(_first(pf, "cash", "cash_balance"), None)
    if cash is None:
        cash = buying_power
    pending_deposits = _f(_first(pf, "pending_deposits"), 0.0) or 0.0
    return {"buying_power": buying_power, "cash": cash, "pending_deposits": pending_deposits}


def parse_positions(snap: dict) -> dict:
    """Parse the positions blob into {SYM: {qty, avg_cost, sellable}} (qty>0 only). avg_cost may be
    None for a position still reconciling; sellable defaults to qty when the broker omits it."""
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
    return positions
