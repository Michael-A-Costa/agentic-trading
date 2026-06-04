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
import sys
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
    # Universe = dynamic discovery (today's top eligible movers) + always-watch pins + held.
    # Discovery replaces a static stock allowlist: a momentum engine has to see what's actually
    # moving market-wide, not a fixed watchlist. The pins (CANDIDATES) stay screened every tick and
    # are the fallback if discovery is disabled/down. See scripts/discover.py for the eligibility filter.
    pinned = [s.strip().upper() for s in env(
        "CANDIDATES", "AAPL,NVDA,TSLA,AMD,MSFT,AMZN,META,GOOGL,F,PLTR").split(",") if s.strip()]
    discovered = []
    if env("DISCOVERY_ENABLED", "1") == "1":
        try:
            import discover  # sibling; keyless Nasdaq screener + eligibility filter, cached per tick
            discovered = discover.discover()
        except Exception as e:  # discovery must NEVER break a tick — fall back to the pins
            sys.stderr.write(f"[tick] discovery failed, using pinned candidates only: {e}\n")
    candidates = list(dict.fromkeys(discovered + pinned))  # ordered de-dupe (movers first)
    held = list(state["positions"].keys())
    # Index ETFs are ALWAYS fetched: SPY is the rel-strength benchmark and the market-data gate
    # needs index data — but they are excluded from entry candidates below (never momentum-buy beta).
    symbols = sorted(set(candidates) | set(held) | set(mc.INDEXES))

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
            "stop_price": p.get("stop_price"), "take_profit_price": p.get("take_profit_price"),
            "entry_ts": p.get("entry_ts"),  # for the max-hold time-exit
            "stop_type": p.get("stop_type", "synthetic"),  # synthetic = engine-tick only, not a resting broker stop
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
            "date": q.get("date"),  # per-symbol freshness (fail-closed: stale candidate is excluded)
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
        "MAX_PER_TRADE_LOSS_USD": envf("MAX_PER_TRADE_LOSS_USD", 60.0),
        "MIN_POSITION_USD": envf("MIN_POSITION_USD", 0.0),  # 0 = no floor; >0 rejects dust fills
    }
    regime = latest_regime()

    # --- DETERMINISTIC screen (rules, no LLM): exits + entry candidates ---
    # Exits are pure risk rules — never a model decision. Stop/TP first, then time-based exits.
    tp, sl = caps["TAKE_PROFIT_PCT"], caps["STOP_LOSS_PCT"]
    sig = caps["SIGNAL_THRESHOLD_PCT"]
    # Screen / risk-mgmt tuning knobs (all .env-overridable; defaults are conservative).
    rel_strength_pct = envf("REL_STRENGTH_PCT", 1.0)   # require this much intraday % ABOVE SPY
    cooldown_min = envf("COOLDOWN_MIN", 30.0)          # no re-entry within N min of an exit (anti-whipsaw)
    flatten_min = envf("FLATTEN_BEFORE_CLOSE_MIN", 15.0)  # flatten all positions N min before close
    no_entry_last_min = envf("NO_ENTRY_LAST_MIN", 15.0)   # block NEW entries in the last N min
    max_hold_min = envf("MAX_HOLD_MIN", 0.0)           # force-exit a position held > N min (0 = off)
    # Minutes until the 16:00 ET close (None when not in a regular session).
    close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    mins_to_close = (close_et - now_et).total_seconds() / 60.0 if is_open else None

    exits = []
    for p in positions:
        lp, pp = p.get("last"), p.get("pnl_pct")
        sp, tpp = p.get("stop_price"), p.get("take_profit_price")
        if lp is None:
            continue  # need a fresh quote to (synthetically) sell against
        reason = None
        # Prefer the explicit per-position stop/TP levels set at buy; fall back to the % rule.
        # "synthetic stop" = enforced here at tick time, NOT a resting broker order (no gap cover).
        if sp is not None and lp <= sp:
            reason = f"synthetic stop hit: {lp} <= stop {sp} ({pp}%)"
        elif tpp is not None and lp >= tpp:
            reason = f"take-profit hit: {lp} >= {tpp} ({pp}%)"
        elif sp is None and pp is not None and pp <= -sl:
            reason = f"stop-loss {pp}% <= -{sl}%"
        elif tpp is None and pp is not None and pp >= tp:
            reason = f"take-profit {pp}% >= {tp}%"
        # Time-based exits (price-independent risk mgmt; bound the synthetic-stop gap window).
        elif is_open and flatten_min > 0 and mins_to_close is not None and mins_to_close <= flatten_min:
            reason = f"EOD flatten ({int(mins_to_close)}m to close)"
        elif max_hold_min > 0 and p.get("entry_ts"):
            try:
                age_min = (now_utc - datetime.fromisoformat(p["entry_ts"])).total_seconds() / 60.0
                if age_min >= max_hold_min:
                    reason = f"max-hold {int(age_min)}m >= {int(max_hold_min)}m"
            except (ValueError, TypeError):
                pass
        if reason:
            exits.append({"symbol": p["symbol"], "reason": reason})

    # Entry candidates: movers clearing BOTH an absolute threshold and a relative-strength bar vs
    # SPY (so we don't just buy market beta on a broad-up tape), in a non-hostile regime, not held,
    # not in post-exit cooldown, with a fresh same-day quote. Ranked by relative strength, then by
    # range position (prefer not-already-at-the-high). Stage 2 (DD) makes the real commit call.
    hostile = (regime.get("posture") == "risk_off") or (regime.get("volatility_regime") == "elevated")
    held = {p["symbol"] for p in positions}
    # A quote is only a LIVE signal during regular hours AND when it carries today's date.
    spy_date = (quotes.get("SPY") or {}).get("date")
    data_stale = (spy_date != today) if spy_date else True
    near_close = bool(no_entry_last_min > 0 and mins_to_close is not None
                      and mins_to_close <= no_entry_last_min)
    allow_entries = bool(is_open and not data_stale and not near_close)
    stale_reason = ("market_not_open" if not is_open
                    else f"stale_quote_date={spy_date}" if data_stale
                    else f"within_{int(no_entry_last_min)}m_of_close" if near_close else "")

    # Symbols still in their post-exit cooldown window (anti-whipsaw; entries only, never exits).
    cooling = set()
    if cooldown_min > 0:
        for s, ts in (state.get("last_exit") or {}).items():
            try:
                if (now_utc - datetime.fromisoformat(ts)).total_seconds() / 60.0 < cooldown_min:
                    cooling.add(s)
            except (ValueError, TypeError):
                continue

    # Never momentum-buy the benchmark / index ETFs (that's just buying beta — the rel-strength
    # bar exists precisely to filter beta out), nor any name on the long-term never-buy exclusion
    # list (DD-flagged structural disqualifiers). Configurable extras via NON_TRADABLE_SYMBOLS.
    try:
        import stock_memory
        excluded = stock_memory.excluded_symbols()
    except Exception:
        excluded = set()
    non_tradable = (set(mc.INDEXES) | excluded
                    | {s.strip().upper() for s in env("NON_TRADABLE_SYMBOLS", "").split(",") if s.strip()})
    entry_candidates = []
    if allow_entries and not hostile:
        spy_move = mc.intraday_pct(quotes.get("SPY") or {})  # SPY already fetched — no extra call
        movers = []
        for c in cand:
            if c["symbol"] in non_tradable:
                continue
            ip = c.get("intraday_pct")
            if ip is None or ip < sig:
                continue
            if c["symbol"] in held or c["symbol"] in cooling:
                continue
            if c.get("date") and c["date"] != today:
                continue  # this symbol's own quote is stale — fail closed, skip it
            rel = round(ip - spy_move, 3) if spy_move is not None else ip
            if rel < rel_strength_pct:
                continue
            movers.append({**c, "rel_strength": rel})
        movers.sort(key=lambda c: (-c["rel_strength"],
                                   c["range_pos"] if c.get("range_pos") is not None else 1.0))
        entry_candidates = [{"symbol": c["symbol"], "intraday_pct": c["intraday_pct"],
                             "rel_strength": c["rel_strength"], "range_pos": c.get("range_pos"),
                             "reason": (f"+{c['intraday_pct']}% intraday, rel {c['rel_strength']} vs SPY "
                                        f">= {rel_strength_pct}, {regime.get('posture')} regime")}
                            for c in movers]
    screen = {"exits": exits, "entry_candidates": entry_candidates,
              "hostile_regime": hostile, "cooling": sorted(cooling)}

    # --- GATE decision (deterministic; the wrapper branches on this) ---
    gate, reason = "TRADE", ""
    if fetch_error or not mc._has_indexes(quotes):  # indexes always fetched; check the full set
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
        "allow_entries": allow_entries,
        "data_stale": data_stale,
        "stale_reason": stale_reason,
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
        "screen": screen,
        "caps": caps,
        "gate": gate,
        "gate_reason": reason,
    }

    # Compact packet for the LLM (only what it needs to decide).
    packet = {
        "mode": context["mode"],
        "allow_entries": allow_entries,   # false => exits/HOLD only; no new positions
        "data_stale": data_stale,
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
