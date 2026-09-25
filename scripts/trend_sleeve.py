#!/usr/bin/env python3
"""
trend_sleeve.py — the BTC trend sleeve: hold IBIT while BTC closes above its 50-day SMA, cash below.

Why this exists: the bake-off (docs/strategy-redesign-2026-09.md, data/bakeoff/crypto_results.md)
found the 50d filter beats BTC buy-and-hold out-of-sample (2022+) ONLY at ETF cost — RH's crypto
markup (~0.95%/side) eats the edge, IBIT shares (~0.01%/side) don't. So the signal comes from clean
Coinbase BTC daily closes and the position is IBIT shares on the agentic account.

Rules (all here in Python; no LLM in the loop):
  - signal: last COMPLETED UTC daily BTC close vs its SMA-n. Above -> long, below -> flat.
  - exposure: vol-targeted, min(1, TREND_VOL_TARGET / 30d realized vol), times the risk-sized cap.
  - risk sizing: loss at the stop = TREND_RISK_BUDGET_PCT of equity -> position = equity x budget / stop.
  - stop: fixed TREND_STOP_PCT below the entry fill, resting stop_market GTC on the whole-share lot.
    Never lowered. If the resting stop can't be armed, a synthetic check sells at market.
  - re-entry after a stop-out: only on a fresh cross (a close below the SMA, then back above).
  - breaker: equity TREND_BREAKER_DD_PCT below its high-water mark -> no new entries/adds.
  - resize (vol target drift) only on Mondays, and only by >= max(1 share, TREND_RESIZE_MIN_FRAC).

Phases: `--phase open` (~09:45 ET) reconciles, then acts on the signal; `--phase close` (~15:50 ET)
only reconciles + guards the stop; `--phase status` (hourly, 24/7) is READ-ONLY — snapshots the account,
flags anomalies (lot without a resting stop, broker/state mismatch, stray positions/orders, breaker,
broker unreachable) and raises a macOS notification on any. TREND_ARMED!=1 is a dry-run: real review, logs intent, places nothing
— and tracks a SHADOW position (state["shadow"]) whose would-be fills go to data/trend-shadow.jsonl, never trades.jsonl.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_execute as lx  # pure order-spec builders, review gate, broker parsing — shared with the old engine
import trade_log

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
STATE_PATH = DATA / "trend_state.json"
LOG_PATH = DATA / "trend-log.jsonl"
ET = ZoneInfo("America/New_York")
CB_URL = "https://api.exchange.coinbase.com/products/{pair}/candles?granularity=86400"
TAG = "[btc-trend]"


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
def _env_f(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def load_cfg() -> dict:
    return {
        "symbol": os.environ.get("TREND_SYMBOL", "IBIT").upper(),
        "pair": os.environ.get("TREND_SIGNAL_PAIR", "BTC-USD"),
        "sma_n": int(_env_f("TREND_SMA_DAYS", 50)),
        "vol_n": int(_env_f("TREND_VOL_DAYS", 30)),
        "vol_target": _env_f("TREND_VOL_TARGET", 0.50),
        "stop_pct": _env_f("TREND_STOP_PCT", 8.0),
        "risk_pct": _env_f("TREND_RISK_BUDGET_PCT", 2.0),
        "max_frac": _env_f("TREND_MAX_POSITION_FRAC", 1.0),
        "breaker_pct": _env_f("TREND_BREAKER_DD_PCT", 15.0),
        "resize_min_frac": _env_f("TREND_RESIZE_MIN_FRAC", 0.20),
        "limit_pct": _env_f("TREND_LIMIT_PCT", 0.5),
        "quote_max_age_min": _env_f("TREND_QUOTE_MAX_AGE_MIN", 20),
    }


def armed() -> bool:
    return str(os.environ.get("TREND_ARMED", "0")).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# PURE rule functions (unit-tested in test_trend_sleeve.py)
# ---------------------------------------------------------------------------
def completed_closes(candles: list, today_utc: date) -> list[tuple[date, float]]:
    """Coinbase candles ([time, low, high, open, close, vol], newest first) -> [(day, close)] oldest
    first, dropping today's still-forming UTC candle so the signal only ever uses a finished close."""
    out = []
    for row in candles:
        d = datetime.fromtimestamp(int(row[0]), tz=timezone.utc).date()
        if d < today_utc:
            out.append((d, float(row[4])))
    out.sort()
    return out


def compute_signal(closes: list[tuple[date, float]], sma_n: int, vol_n: int, vol_target: float) -> dict:
    """Signal + vol-target weight from the latest completed close. Matches bakeoff_crypto: SMA over the
    last n closes (inclusive), vol = stdev of the last vol_n daily returns x sqrt(365)."""
    if len(closes) < max(sma_n, vol_n + 1):
        return {"ok": False, "reason": f"need {max(sma_n, vol_n + 1)} closes, have {len(closes)}"}
    px = [c for _, c in closes]
    sma = sum(px[-sma_n:]) / sma_n
    rets = [px[i] / px[i - 1] - 1 for i in range(len(px) - vol_n, len(px))]
    mu = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(365)
    on = px[-1] > sma
    weight = min(1.0, vol_target / vol) if vol > 0 else 0.0
    return {"ok": True, "date": closes[-1][0].isoformat(), "close": px[-1], "sma": round(sma, 2),
            "ext_pct": round((px[-1] / sma - 1) * 100, 2), "on": on, "vol": round(vol, 4),
            "weight": round(weight if on else 0.0, 4)}


def update_lock(lock: dict | None, on: bool) -> dict | None:
    """Re-entry lock after a stop-out: cleared only once the signal has been OFF and then ON again."""
    if lock is None:
        return None
    if not on:
        return {**lock, "seen_below": True}
    return None if lock.get("seen_below") else lock


def target_shares(*, equity: float, weight: float, price: float, cfg: dict, spendable: float) -> tuple[int, str]:
    """Whole IBIT shares to hold. Risk-sized cap (equity x budget / stop distance, <= max_frac x equity)
    scaled by the vol weight, bounded by spendable cash for any increase (caller passes current value
    + settled cash). Whole shares only: only a whole-share lot can carry a resting stop at RH."""
    if weight <= 0 or price <= 0 or equity <= 0:
        return 0, "flat"
    cap = min(equity * (cfg["risk_pct"] / 100.0) / (cfg["stop_pct"] / 100.0), equity * cfg["max_frac"])
    budget = min(cap * weight, spendable)
    buy_px = price * (1 + cfg["limit_pct"] / 100.0)
    qty = int(math.floor(budget / buy_px + 1e-9))
    note = f"cap=${cap:.2f} x w={weight:.2f} -> ${cap * weight:.2f}; spendable=${spendable:.2f}"
    return qty, note


def stop_level(entry: float, stop_pct: float) -> float:
    return round(entry * (1 - stop_pct / 100.0), 2)


def breaker_tripped(equity: float, hwm: float | None, pct: float) -> bool:
    return bool(hwm) and equity < hwm * (1 - pct / 100.0)


def resize_warranted(cur: int, want: int, is_monday: bool, min_frac: float) -> bool:
    if not is_monday or cur <= 0 or want <= 0 or want == cur:
        return False
    return abs(want - cur) >= max(1, math.ceil(cur * min_frac))


def next_business_day(d: date) -> date:
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n


def settled_cash(buying_power: float, unsettled: list, today: date) -> float:
    """Cash account: our own sale proceeds aren't spendable until T+1 (a GFV otherwise). The broker's
    buying_power has been seen to include them, so subtract what we sold that hasn't settled."""
    pending = sum(u["amount"] for u in unsettled if date.fromisoformat(u["settles"]) > today)
    return max(0.0, buying_power - pending)


def shadow_stop_hit(lot: dict, bars: list[dict]) -> dict | None:
    """First bar (oldest first) that reaches the stop -> {"ts", "price"}. A bar that OPENS below the stop
    (overnight/weekend gap) fills at its open, worse than the stop — what a resting stop_market does."""
    for b in bars:
        if b["low"] <= lot["stop_price"]:
            return {"ts": b["ts"], "price": round(min(lot["stop_price"], b["open"]), 4)}
    return None


def shadow_step(sh: dict | None, *, phase: str, bars: list[dict] | None, sig: dict | None,
                entry_ok: bool, want: int, quote: dict, cfg: dict, now: datetime) -> tuple[dict, list[dict]]:
    """Dry-run SHADOW position: what the armed sleeve would be holding, so would-be trades pair into
    round-trips instead of every dry-run morning logging a fresh 'entry'. Same rules as the real lot:
    stop checked on every bar since the last run, signal exit at the open run, re-entry after a stop-out
    only on a fresh cross. Returns (new shadow state, would-be fills as trade_log results)."""
    sh = {"lot": None, "lock": None, **(sh or {})}
    rows: list[dict] = []
    stamp = {"ts_utc": now.isoformat(timespec="seconds"), "ts_et": now.astimezone(ET).isoformat(timespec="seconds")}

    def close(px: float, why: str, when: dict) -> None:
        lot = sh["lot"]
        rows.append({**when, "symbol": cfg["symbol"], "side": "sell", "status": "shadow", "qty": lot["qty"],
                     "price": px, "reason": f"{TAG} {why}",
                     "realized_usd": round((px - lot["entry_price"]) * lot["qty"], 2)})
        sh["lot"] = None

    if sh["lot"] and bars:
        hit = shadow_stop_hit(sh["lot"], bars)
        if hit:
            t = parse_ts(hit["ts"])
            close(hit["price"], f"stop-loss (resting stop, bar {hit['ts']})",
                  {"ts_utc": t.isoformat(timespec="seconds"), "ts_et": t.astimezone(ET).isoformat(timespec="seconds")})
            sh["lock"] = {"since": t.astimezone(ET).date().isoformat(), "seen_below": False}
    if phase == "open" and sig and sig.get("ok"):
        sh["lock"] = update_lock(sh["lock"], sig["on"])
        if sh["lot"] and not sig["on"]:
            px = quote.get("bid") or quote.get("last")
            if px:
                close(px, "signal exit (BTC close below SMA)", stamp)
        elif not sh["lot"] and entry_ok and want > 0 and sh["lock"] is None:
            px = quote.get("ask") or quote.get("last")
            if px:
                sh["lot"] = {"qty": want, "entry_price": px, "stop_price": stop_level(px, cfg["stop_pct"]),
                             "entry_ts": stamp["ts_utc"]}
                sh["checked_through"] = stamp["ts_utc"]
                rows.append({**stamp, "symbol": cfg["symbol"], "side": "buy", "status": "shadow", "qty": want,
                             "price": px, "ref_price": px, "stop_price": sh["lot"]["stop_price"],
                             "stop_type": "resting",
                             "reason": f"{TAG} signal entry (BTC {sig['ext_pct']}% vs SMA{cfg['sma_n']})"})
    return sh, rows


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------
def fetch_candles(pair: str) -> list:
    req = urllib.request.Request(CB_URL.format(pair=pair), headers={"User-Agent": "agentic-trading-trend/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_bars(sym: str, since: datetime, now: datetime) -> list[dict] | None:
    """5-min RTH bars that END after `since`, oldest first, as {ts, open, low}; None if the fetch failed
    (caller then leaves its checkpoint alone so the next run re-scans the gap). Clamped to 20 days back
    so an explicit 5-minute request stays under the upstream bar cap."""
    import rh_direct
    start = max(since, now - timedelta(days=20))
    try:
        raw = rh_direct.historicals([sym], start.strftime("%Y-%m-%dT%H:%M:%SZ"), "5minute")
    except Exception as e:
        print(f"[shadow] bars fetch failed: {e}", file=sys.stderr)
        return None
    out = []
    for r in (lx._unwrap(raw.get("bars") or {}) or {}).get("results") or []:
        for b in r.get("bars") or []:
            t = parse_ts(b["begins_at"])
            if b.get("interpolated") or t + timedelta(minutes=5) <= since:
                continue
            out.append({"ts": b["begins_at"], "open": float(b["open_price"]), "low": float(b["low_price"])})
    out.sort(key=lambda b: b["ts"])
    return out


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"lot": None, "lock": None, "hwm": None, "unsettled": []}


def manual_symbols() -> list[str]:
    """Symbols the owner holds by hand (`book: manual` live rows in trades.jsonl). They share the account
    with the sleeve but aren't its to trade, so the checks must count them without calling them strays."""
    out = set()
    try:
        with trade_log.TRADES_LOG.open() as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("book") == "manual" and row.get("mode") == "live" and row.get("symbol"):
                    out.add(str(row["symbol"]).upper())
    except OSError:
        pass
    return sorted(out)


def account_equity(broker: dict) -> float:
    """PURE: cash + every position marked to its quote (avg cost when unquoted). Valuing only quoted
    symbols once priced a manual holding at $0 and tripped the breaker on a flat account."""
    total = broker["cash"]
    for s, p in broker["positions"].items():
        px = broker["quotes"].get(s, {}).get("last") or p.get("avg_cost") or 0.0
        total += (p.get("qty") or 0.0) * px
    return total


def log_event(ev: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(ev, default=str) + "\n")


def parse_ts(t: str) -> datetime:
    """RH timestamps carry nanoseconds ('...59.527129485Z'); fromisoformat takes at most micros."""
    t = t.replace("Z", "+00:00")
    if "." in t:
        head, rest = t.split(".", 1)
        frac, tz = (rest.split("+", 1) + [""])[:2] if "+" in rest else (rest, "")
        t = f"{head}.{frac[:6].ljust(6, '0')}" + (f"+{tz}" if tz else "")
    ts = datetime.fromisoformat(t)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def quote_fresh(raw_quotes: dict | None, sym: str, now: datetime, max_age_min: float) -> tuple[bool, str]:
    """Market-open check without a holiday calendar: the regular-session last trade must be recent."""
    res = (lx._unwrap(raw_quotes or {}) or {}).get("results") or []
    for item in res:
        q = item.get("quote", item) if isinstance(item, dict) else {}
        if str(q.get("symbol", "")).upper() == sym:
            t = q.get("venue_last_trade_time")
            if not t:
                return False, "no venue_last_trade_time"
            ts = parse_ts(t)
            age = (now - ts).total_seconds() / 60
            return age <= max_age_min, f"last regular trade {age:.0f} min ago"
    return False, "symbol missing from quotes"


# ---------------------------------------------------------------------------
# broker actions (dry-run aware)
# ---------------------------------------------------------------------------
def _place(spec: dict, run: dict, what: str) -> dict | None:
    """review -> (armed) place. Returns the order object, or None on dry-run / block / failure."""
    import rh_mcp
    review = rh_mcp.review(spec)
    blocking = lx.review_blocking(review)
    ev = {"event": what, "spec": spec, "alerts": blocking}
    if blocking:
        run["events"].append({**ev, "status": "blocked"})
        return None
    if not armed():
        run["events"].append({**ev, "status": "dryrun"})
        return None
    ref_id = str(uuid.uuid4())
    placed = rh_mcp.place(spec, ref_id=ref_id)
    order = lx.order_obj(placed)
    oid = lx._first(order, "id", "order_id") if isinstance(order, dict) else None
    if not oid and not (isinstance(placed, dict) and placed.get("errors")) and spec.get("side") == "buy":
        order = lx._confirm_recent_buy(spec["symbol"], run["events"])
        oid = lx._first(order, "id", "order_id") if order else None
    run["events"].append({**ev, "status": "placed" if oid else "failed", "order_id": oid, "ref_id": ref_id,
                          "result": None if oid else placed})
    return order if oid else None


def _cancel_stops(sym: str, orders: list, run: dict) -> None:
    import rh_mcp
    for o in lx.open_stops_for(orders, sym):
        oid = lx._first(o, "id", "order_id")
        if not oid:
            continue
        if armed():
            rh_mcp.cancel(oid)
        run["events"].append({"event": "cancel_stop", "order_id": oid, "status": "done" if armed() else "dryrun"})


def _arm_stop(sym: str, lot: dict, orders: list, run: dict) -> None:
    """Ensure exactly one resting stop for the full lot at lot['stop_price']."""
    stops = lx.open_stops_for(orders, sym)
    good = [o for o in stops if int(lx._f(o.get("quantity"), 0) or 0) == int(lot["qty"])
            and abs((lx._f(o.get("stop_price"), 0) or 0) - lot["stop_price"]) < 0.005]
    if good and len(stops) == 1:
        lot["stop_order_id"] = lx._first(good[0], "id", "order_id")
        return
    _cancel_stops(sym, orders, run)
    o = _place(lx.stop_spec(sym, lot["qty"], lot["stop_price"]), run, "arm_stop")
    lot["stop_order_id"] = lx._first(o, "id", "order_id") if o else None


def _fill_of(sym: str, order: dict, tries: int = 4, wait_s: float = 3.0) -> tuple[int, float | None]:
    """A marketable limit usually fills within seconds; poll briefly so the stop is armed in THIS run
    rather than leaving the lot naked until the afternoon check."""
    oid = str(lx._first(order, "id", "order_id") or "")
    qty, avg = 0, None
    for i in range(tries):
        fresh = lx._read_order(sym, oid) or order
        qty = int(math.floor(lx._f(lx._first(fresh, "cumulative_quantity", "filled_quantity"), 0.0) or 0.0))
        avg = lx._f(lx._first(fresh, "average_price"))
        if qty >= 1 and str(fresh.get("state", "")).lower() == "filled":
            break
        if i < tries - 1:
            time.sleep(wait_s)
    return qty, avg


def _sell(sym: str, qty: int, reason: str, broker: dict, cfg: dict, state: dict, run: dict, today: date) -> None:
    _cancel_stops(sym, broker["orders"], run)
    spec = lx.sell_spec(sym, qty, whole=True, quote=broker["quotes"].get(sym, {}), caps={}, urgent=True)
    order = _place(spec, run, "sell")
    lot = state["lot"] or {}
    px = broker["quotes"].get(sym, {}).get("bid") or broker["quotes"].get(sym, {}).get("last")
    res = {"symbol": sym, "side": "sell", "reason": f"{TAG} {reason}", "qty": qty, "price": px,
           "status": "placed" if order else ("dryrun" if not armed() else "failed"),
           "order_spec": spec}
    if order:
        if lot.get("entry_price") and px:
            res["realized_est_usd"] = round((px - lot["entry_price"]) * qty, 2)
        state["unsettled"].append({"amount": round(qty * (px or 0), 2),
                                   "settles": next_business_day(today).isoformat()})
        remaining = int(lot.get("qty", 0)) - qty
        state["lot"] = None if remaining <= 0 else {**lot, "qty": remaining}
    run["results"].append(res)


def _buy(sym: str, qty: int, reason: str, broker: dict, cfg: dict, state: dict, run: dict) -> None:
    q = broker["quotes"].get(sym, {})
    ref = q.get("ask") or q.get("last")
    limit = round(ref * (1 + cfg["limit_pct"] / 100.0), 2)
    spec = {"symbol": sym, "side": "buy", "type": "limit", "quantity": str(qty),
            "limit_price": f"{limit:.2f}", "time_in_force": "gfd", "market_hours": "regular_hours"}
    order = _place(spec, run, "buy")
    res = {"symbol": sym, "side": "buy", "reason": f"{TAG} {reason}", "qty": qty, "price": limit,
           "status": "placed" if order else ("dryrun" if not armed() else "failed"), "order_spec": spec}
    if order:
        filled, avg = _fill_of(sym, order)
        lot = state["lot"]
        if filled >= 1:
            if lot:  # add to an existing lot: keep the ORIGINAL entry's stop (never lowered)
                lot["qty"] = int(lot["qty"]) + filled
            else:
                entry = avg or limit
                lot = state["lot"] = {"qty": filled, "entry_price": entry,
                                      "stop_price": stop_level(entry, cfg["stop_pct"]),
                                      "entry_ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            res.update(status="filled", qty=filled, price=avg or limit, stop_price=lot["stop_price"])
            _arm_stop(sym, lot, broker["orders"], run)  # an add must replace the old lot's stop
            res["stop_type"] = "resting" if lot.get("stop_order_id") else "synthetic"
        else:
            run["events"].append({"event": "buy_unfilled_in_run", "order_id": lx._first(order, "id")})
    run["results"].append(res)


def _shadow(sym: str, phase: str, broker: dict, cfg: dict, state: dict, run: dict, now: datetime) -> None:
    """Dry-run only: advance the shadow position and log its would-be fills (trade_log.record_shadow)."""
    sh = state.get("shadow") or {}
    bars = None
    if sh.get("lot"):
        bars = fetch_bars(sym, parse_ts(sh.get("checked_through") or sh["lot"]["entry_ts"]), now)
        if bars is None:
            run["events"].append({"event": "shadow_bars_failed"})
        else:
            sh["checked_through"] = now.isoformat(timespec="seconds")
    # enter only when the real review passed (a blocked review would have stopped the armed buy too)
    entry_ok = run.get("action") == "entry" and any(
        e.get("event") == "buy" and e.get("status") == "dryrun" for e in run["events"])
    sh, rows = shadow_step(sh, phase=phase, bars=bars, sig=run.get("signal") if phase == "open" else None,
                           entry_ok=entry_ok, want=int(run.get("want") or 0),
                           quote=broker["quotes"].get(sym, {}), cfg=cfg, now=now)
    state["shadow"] = sh
    run["shadow"] = {"lot": sh["lot"], "lock": sh["lock"], "fills": rows}
    trade_log.record_shadow(rows)


# ---------------------------------------------------------------------------
# reconcile: broker truth wins
# ---------------------------------------------------------------------------
def reconcile(sym: str, broker: dict, cfg: dict, state: dict, run: dict, today: date) -> None:
    held = int(math.floor(broker["positions"].get(sym, {}).get("qty", 0.0) or 0.0))
    lot = state["lot"]
    if lot and held == 0:
        # position gone -> most likely the resting stop filled (or an owner-side sale)
        stopped = False
        if lot.get("stop_order_id"):
            since = parse_ts(lot["entry_ts"]) if lot.get("entry_ts") else datetime.now(timezone.utc) - timedelta(days=90)
            lookback = int((datetime.now(timezone.utc) - since).total_seconds()) + 86400
            o = lx._read_order(sym, lot["stop_order_id"], lookback_s=lookback)
            stopped = bool(o) and str(o.get("state", "")).lower() == "filled"
            px = lx._f(lx._first(o or {}, "average_price"))
        else:
            px = None
        if stopped:
            state["lock"] = {"since": today.isoformat(), "seen_below": False}
            state["unsettled"].append({"amount": round((px or 0) * lot["qty"], 2),
                                       "settles": next_business_day(today).isoformat()})
        run["results"].append({"symbol": sym, "side": "sell", "status": "filled", "qty": lot["qty"],
                               "price": px, "reason": f"{TAG} {'stop-loss (resting stop filled)' if stopped else 'external close'}",
                               **({"realized_est_usd": round((px - lot['entry_price']) * lot['qty'], 2)} if px else {})})
        state["lot"] = None
        return
    if held and not lot:
        avg = broker["positions"][sym].get("avg_cost") or broker["quotes"].get(sym, {}).get("last")
        lot = state["lot"] = {"qty": held, "entry_price": avg, "stop_price": stop_level(avg, cfg["stop_pct"]),
                              "entry_ts": None, "adopted": True}
        run["events"].append({"event": "adopted_position", "qty": held, "avg_cost": avg})
    if lot:
        if held != int(lot["qty"]):
            run["events"].append({"event": "qty_resync", "state_qty": lot["qty"], "broker_qty": held})
            lot["qty"] = held
        last = broker["quotes"].get(sym, {}).get("last")
        _arm_stop(sym, lot, broker["orders"], run)
        if not lot.get("stop_order_id") and last is not None and last <= lot["stop_price"]:
            _sell(sym, held, f"stop-loss (synthetic, last {last} <= {lot['stop_price']})",
                  broker, cfg, state, run, today)


# ---------------------------------------------------------------------------
# hourly status (read-only)
# ---------------------------------------------------------------------------
def status_anomalies(sym: str, broker: dict, state: dict, cfg: dict, equity: float,
                     manual: list[str] | tuple = ()) -> list[str]:
    """PURE: what's wrong with the account right now, as short human-readable strings. `manual` symbols
    are owner-held, not strays — but they still need a resting stop on their whole shares."""
    out = []
    held = int(math.floor(broker["positions"].get(sym, {}).get("qty", 0.0) or 0.0))
    stops = lx.open_stops_for(broker["orders"], sym)
    lot = state.get("lot")
    if held and not stops:
        out.append(f"{sym} x{held} has NO resting stop")
    for o in stops:
        q = int(lx._f(o.get("quantity"), 0) or 0)
        if q != held:
            out.append(f"stop qty {q} != held {held}")
    if lot and int(lot.get("qty", 0)) != held:
        out.append(f"state lot qty {lot.get('qty')} != broker {held}")
    if held and not lot:
        out.append(f"broker holds {sym} x{held} but no tracked lot")
    for m in sorted(set(manual) & set(broker["positions"])):
        whole = int(math.floor(broker["positions"][m].get("qty") or 0.0))
        covered = sum(int(lx._f(o.get("quantity"), 0) or 0) for o in lx.open_stops_for(broker["orders"], m))
        if whole and covered < whole:  # a fraction can't carry a broker stop, so only whole shares count
            out.append(f"{m} x{whole} (manual) has only {covered} under a resting stop")
    stray = sorted(s for s in broker["positions"] if s != sym and s not in manual)
    if stray:
        out.append(f"unexpected positions: {', '.join(stray)}")
    if breaker_tripped(equity, state.get("hwm"), cfg["breaker_pct"]):
        out.append(f"breaker: equity ${equity:.2f} is {cfg['breaker_pct']}%+ below high-water ${state['hwm']:.2f}")
    return out


def applescript_str(s: str) -> str:
    """AppleScript string literal. Only backslash and double-quote need escaping; json.dumps is NOT a
    substitute — it escapes non-ASCII (an em dash becomes backslash-u2014), which AppleScript rejects
    as a syntax error."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, msg: str) -> bool:
    """Best-effort macOS banner; never let alerting break the check, but never fail silently either."""
    import subprocess
    try:
        r = subprocess.run(["osascript", "-e", f"display notification {applescript_str(msg)} "
                                               f"with title {applescript_str(title)}"],
                           timeout=10, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[notify] failed: {e}", file=sys.stderr)
        return False
    if r.returncode != 0:
        print(f"[notify] osascript rc={r.returncode}: {r.stderr.strip()}", file=sys.stderr)
    return r.returncode == 0


def status(cfg: dict, now: datetime) -> int:
    import rh_mcp
    sym = cfg["symbol"]
    rec = {"ts_utc": now.isoformat(timespec="seconds"), "phase": "status", "armed": armed()}
    manual = manual_symbols()
    snap = rh_mcp.snapshot([sym, *manual])
    if not snap:
        rec["anomalies"] = ["broker snapshot FAILED (MCP auth/token or network)"]
    else:
        broker = lx.parse_snapshot(snap)
        state = load_state()
        last = broker["quotes"].get(sym, {}).get("last") or 0.0
        held = int(math.floor(broker["positions"].get(sym, {}).get("qty", 0.0) or 0.0))
        equity = account_equity(broker)
        lot = state.get("lot") or {}
        rec.update(equity=round(equity, 2), cash=broker["cash"], buying_power=broker["buying_power"],
                   held=held, last=last, stop=lot.get("stop_price"), entry=lot.get("entry_price"),
                   open_orders=len(broker["orders"]), hwm=state.get("hwm"),
                   manual={m: broker["positions"][m]["qty"] for m in manual if m in broker["positions"]},
                   anomalies=status_anomalies(sym, broker, state, cfg, equity, manual))
        if not armed() and (state.get("shadow") or {}).get("lot"):
            rec["shadow_lot"] = state["shadow"]["lot"]
        if held and lot.get("entry_price"):
            rec["unrealized_usd"] = round((last - lot["entry_price"]) * held, 2)
            rec["to_stop_pct"] = round((last / lot["stop_price"] - 1) * 100, 2) if lot.get("stop_price") else None
        try:
            sig = compute_signal(completed_closes(fetch_candles(cfg["pair"]), now.date()),
                                 cfg["sma_n"], cfg["vol_n"], cfg["vol_target"])
            rec["signal"] = {k: sig.get(k) for k in ("date", "close", "sma", "ext_pct", "on")}
        except Exception as e:
            rec["signal"] = {"error": str(e)[:120]}
    log_event(rec)
    line = (f"equity ${rec.get('equity')} | {sym} x{rec.get('held')} @ {rec.get('last')}"
            f" | stop {rec.get('stop')} | signal {'ON' if (rec.get('signal') or {}).get('on') else 'off'}"
            f" ({(rec.get('signal') or {}).get('ext_pct')}% vs SMA)")
    print(f"[status] {line}" + (f" | ANOMALIES: {rec['anomalies']}" if rec.get("anomalies") else " | ok"))
    if rec.get("anomalies"):
        notify("Trading bot: check account", "; ".join(rec["anomalies"])[:220])
    return 1 if rec.get("anomalies") else 0


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="BTC trend sleeve (IBIT) — one scheduled check-in")
    ap.add_argument("--phase", choices=["open", "close", "status"], required=True)
    ap.add_argument("--ignore-hours", action="store_true",
                    help="dry-run testing only: skip the market-open check (refused when armed)")
    args = ap.parse_args()
    if args.ignore_hours and armed():
        print("--ignore-hours is dry-run only; refusing with TREND_ARMED=1", file=sys.stderr)
        return 2
    import rh_mcp

    cfg = load_cfg()
    sym = cfg["symbol"]
    now = datetime.now(timezone.utc)
    if args.phase == "status":
        return status(cfg, now)
    today = now.astimezone(ET).date()
    run = {"ts_utc": now.isoformat(timespec="seconds"), "phase": args.phase, "armed": armed(),
           "events": [], "results": []}

    snap = rh_mcp.snapshot([sym, *manual_symbols()])
    if not snap:
        run.update(action="skip", reason="broker snapshot failed")
        log_event(run)
        return 1
    broker = lx.parse_snapshot(snap)
    fresh, why = quote_fresh(snap.get("quotes"), sym, now, cfg["quote_max_age_min"])
    run["market"] = why
    state = load_state()
    state["unsettled"] = [u for u in state["unsettled"] if date.fromisoformat(u["settles"]) > today]
    if armed() and state.get("shadow"):  # real money from here on; the dry-run shadow must not linger
        run["events"].append({"event": "shadow_cleared", "shadow": state.pop("shadow")})

    last = broker["quotes"].get(sym, {}).get("last") or 0.0
    held = int(math.floor(broker["positions"].get(sym, {}).get("qty", 0.0) or 0.0))
    equity = account_equity(broker)  # whole account: owner-held (manual) money moved out of cash isn't a sleeve loss
    state["hwm"] = max(state.get("hwm") or 0.0, equity)
    run.update(equity=round(equity, 2), hwm=round(state["hwm"], 2), held=held, last=last,
               buying_power=broker["buying_power"])

    if not fresh and not args.ignore_hours:
        run.update(action="skip", reason=f"market not open ({why})")
        print(json.dumps({"phase": args.phase, "action": "skip", "reason": run["reason"]}))
        log_event(run)
        STATE_PATH.write_text(json.dumps(state, indent=2))
        return 0

    reconcile(sym, broker, cfg, state, run, today)

    if args.phase == "open":
        try:
            candles = fetch_candles(cfg["pair"])
        except Exception as e:  # no signal -> no new decisions; the resting stop still protects the lot
            candles = []
            run["events"].append({"event": "signal_fetch_failed", "error": str(e)[:200]})
        sig = compute_signal(completed_closes(candles, now.date()),
                             cfg["sma_n"], cfg["vol_n"], cfg["vol_target"])
        run["signal"] = sig
        if not sig["ok"]:
            run.update(action="skip", reason=sig["reason"])
        else:
            state["lock"] = update_lock(state.get("lock"), sig["on"])
            cur = int(state["lot"]["qty"]) if state["lot"] else 0
            spendable = cur * last + settled_cash(broker["buying_power"], state["unsettled"], today)
            want, note = target_shares(equity=equity, weight=sig["weight"], price=last, cfg=cfg,
                                       spendable=spendable)
            if state["lock"]:
                want, note = 0, "re-entry locked until a fresh cross"
            tripped = breaker_tripped(equity, state["hwm"], cfg["breaker_pct"])
            run.update(want=want, sizing=note, lock=state["lock"], breaker=tripped)
            if cur > 0 and want == 0:
                _sell(sym, cur, "signal exit (BTC close below SMA)" if not sig["on"] else note,
                      broker, cfg, state, run, today)
                run["action"] = "exit"
            elif cur == 0 and want > 0:
                if tripped:
                    run.update(action="skip", reason="breaker: equity below high-water limit")
                else:
                    _buy(sym, want, f"signal entry (BTC {sig['ext_pct']}% vs SMA{cfg['sma_n']})",
                         broker, cfg, state, run)
                    run["action"] = "entry"
            elif resize_warranted(cur, want, today.weekday() == 0, cfg["resize_min_frac"]):
                if want > cur and not tripped:
                    _buy(sym, want - cur, "vol-target resize (add)", broker, cfg, state, run)
                    run["action"] = "resize_add"
                elif want < cur:
                    _sell(sym, cur - want, "vol-target resize (trim)", broker, cfg, state, run, today)
                    if state["lot"]:
                        _arm_stop(sym, state["lot"], [], run)
                    run["action"] = "resize_trim"
            else:
                run.setdefault("action", "hold" if cur else "flat")

    if not armed() and not args.ignore_hours:  # --ignore-hours runs are off-session tests, not sessions
        _shadow(sym, args.phase, broker, cfg, state, run, now)
    run["lot"] = state["lot"]
    trade_log.record_fills(run["results"], ts_utc=run["ts_utc"], ts_et=now.astimezone(ET).isoformat(timespec="seconds"),
                           mode="live")
    log_event(run)
    STATE_PATH.write_text(json.dumps(state, indent=2))
    print(json.dumps({k: run.get(k) for k in ("phase", "armed", "market", "action", "reason", "equity",
                                                "held", "want", "sizing", "signal", "lot")}, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
