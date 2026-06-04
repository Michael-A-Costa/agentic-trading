#!/usr/bin/env python3
"""
tick_context.py — deterministic context-gatherer for one trading tick (PAPER mode).

Does ALL the gathering so the LLM doesn't have to (saves tokens): reads the latest market
regime, the paper portfolio, fresh public quotes for held + candidate symbols, computes
per-position P&L and the day's P&L, and decides the GATE (trade vs skip) including the
daily-loss circuit breaker. Writes:
  - data/tick/context_latest.json    full snapshot (for logging/audit)
  - data/tick/packet_latest.json     compact packet (this is what the LLM sees)
and prints a final `GATE=TRADE` / `GATE=SKIP:<reason>` line for the wrapper to branch on.

Paper mode uses public quotes + a locally-tracked portfolio, so NO Robinhood MCP is needed.
Config comes from the environment (the wrapper sources .env); sane defaults if unset.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import market_conditions as mc  # sibling module; its dir is on sys.path when run as a script

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
TICK = DATA / "tick"
STATE_PATH = DATA / "paper_state.json"
REGIME_LOG = DATA / "market_conditions.jsonl"


def env(key: str, default: str) -> str:
    v = os.environ.get(key)
    return v if v not in (None, "") else default


def envf(key: str, default: float) -> float:
    try:
        return float(env(key, str(default)))
    except ValueError:
        return default


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {
        "cash": envf("PAPER_START_CASH", 3000.0),
        "positions": {},          # SYM -> {qty, entry_price, entry_ts}
        "realized_total": 0.0,
        "day": None,
        "start_of_day_equity": None,
    }


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def latest_regime() -> dict:
    if not REGIME_LOG.exists():
        return {}
    last = None
    for line in REGIME_LOG.read_text().splitlines():
        if line.strip():
            last = line
    if not last:
        return {}
    try:
        rec = json.loads(last)
    except json.JSONDecodeError:
        return {}
    a = rec.get("assessment") or {}
    return {
        "session": rec.get("session"),
        "market_open": rec.get("market_open"),
        "posture": a.get("posture"),
        "volatility_regime": a.get("volatility_regime"),
        "breadth_regime": a.get("breadth_regime"),
        "avg_index_move_pct": a.get("avg_index_move_pct"),
        "vix_proxy_move_pct": a.get("vix_proxy_move_pct"),
        "source": rec.get("source"),
        "ts_et": rec.get("ts_et"),
    }


def main() -> int:
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(mc.ET)
    today = now_et.strftime("%Y-%m-%d")
    session, is_open = mc.session_state(now_et)
    allow_offhours = env("ALLOW_OFFHOURS", "0") == "1"

    state = load_state()
    candidates = [s.strip().upper() for s in env(
        "CANDIDATES", "AAPL,NVDA,TSLA,AMD,MSFT,AMZN,META,GOOGL,SPY,QQQ,F,PLTR").split(",") if s.strip()]
    held = list(state["positions"].keys())
    symbols = sorted(set(candidates) | set(held))

    quotes, source = {}, None
    fetch_error = None
    try:
        quotes, source = mc.fetch_quotes(symbols)
    except (OSError, RuntimeError) as e:
        fetch_error = str(e)

    def last(sym):
        return (quotes.get(sym) or {}).get("last")

    # --- positions with live P&L ---
    positions = []
    pos_value = 0.0
    for sym, p in state["positions"].items():
        lp = last(sym)
        qty = p["qty"]
        entry = p["entry_price"]
        val = (lp or entry) * qty
        pos_value += val
        pnl = (lp - entry) * qty if lp else 0.0
        pnl_pct = round((lp - entry) / entry * 100, 2) if lp and entry else None
        positions.append({
            "symbol": sym, "qty": round(qty, 6), "entry_price": entry,
            "last": lp, "value": round(val, 2), "pnl_usd": round(pnl, 2), "pnl_pct": pnl_pct,
        })

    equity = round(state["cash"] + pos_value, 2)

    # --- day rollover + day P&L for the circuit breaker ---
    if state.get("day") != today or state.get("start_of_day_equity") is None:
        state["day"] = today
        state["start_of_day_equity"] = equity
        save_state(state)
    day_pnl = round(equity - state["start_of_day_equity"], 2)

    # --- candidates with intraday move ---
    cand = []
    for sym in candidates:
        q = quotes.get(sym) or {}
        cand.append({
            "symbol": sym, "last": q.get("last"),
            "intraday_pct": mc.intraday_pct(q) if q else None,
            "range_pos": mc.range_position(q) if q else None,
        })

    caps = {
        "MAX_POSITION_USD": envf("MAX_POSITION_USD", 600.0),
        "MAX_TOTAL_EXPOSURE_USD": envf("MAX_TOTAL_EXPOSURE_USD", 2400.0),
        "MAX_OPEN_POSITIONS": int(envf("MAX_OPEN_POSITIONS", 6)),
        "MAX_SYMBOL_WEIGHT": envf("MAX_SYMBOL_WEIGHT", 0.25),
        "STOP_LOSS_PCT": envf("STOP_LOSS_PCT", 2.0),
        "TAKE_PROFIT_PCT": envf("TAKE_PROFIT_PCT", 4.0),
        "SIGNAL_THRESHOLD_PCT": envf("SIGNAL_THRESHOLD_PCT", 2.0),
        "DAILY_MAX_LOSS_USD": envf("DAILY_MAX_LOSS_USD", 150.0),
    }
    regime = latest_regime()

    # --- GATE decision (deterministic; the wrapper branches on this) ---
    gate, reason = "TRADE", ""
    if fetch_error or not mc._has_indexes({s: quotes.get(s, {}) for s in candidates}):
        gate, reason = "SKIP", f"no_market_data ({fetch_error or 'empty quotes'})"
    elif day_pnl <= -caps["DAILY_MAX_LOSS_USD"]:
        gate, reason = "SKIP", f"circuit_breaker day_pnl={day_pnl} <= -{caps['DAILY_MAX_LOSS_USD']}"
    elif not is_open and not allow_offhours:
        gate, reason = "SKIP", f"market_{session}"

    context = {
        "ts_utc": now_utc.isoformat(timespec="seconds"),
        "ts_et": now_et.isoformat(timespec="seconds"),
        "mode": env("TRADING_MODE", "paper"),
        "session": session,
        "market_open": is_open,
        "allow_offhours": allow_offhours,
        "quote_source": source,
        "regime": regime,
        "portfolio": {
            "cash": round(state["cash"], 2),
            "positions_value": round(pos_value, 2),
            "equity": equity,
            "start_of_day_equity": state["start_of_day_equity"],
            "day_pnl": day_pnl,
            "realized_total": round(state["realized_total"], 2),
            "open_positions": len(positions),
        },
        "positions": positions,
        "candidates": cand,
        "caps": caps,
        "gate": gate,
        "gate_reason": reason,
    }

    # Compact packet for the LLM (only what it needs to decide).
    packet = {
        "mode": context["mode"],
        "regime": {k: regime.get(k) for k in
                   ("posture", "volatility_regime", "breadth_regime", "session")},
        "portfolio": context["portfolio"],
        "positions": [{k: p[k] for k in ("symbol", "qty", "entry_price", "last", "pnl_pct")}
                      for p in positions],
        "candidates": [c for c in cand if c["last"] is not None],
        "caps": caps,
    }

    TICK.mkdir(parents=True, exist_ok=True)
    (TICK / "context_latest.json").write_text(json.dumps(context, indent=2))
    (TICK / "packet_latest.json").write_text(json.dumps(packet))

    print(f"GATE={gate}" + (f":{reason}" if reason else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
