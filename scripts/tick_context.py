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
import hold_risk                # sibling: deterministic Tier-1 risk score for open positions

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


def scale_out_tiers() -> list[tuple[float, float]]:
    """Parse SCALE_OUT_TIERS ("gain%:fracOfInitQty, ...") into sorted (gain, frac) pairs.

    e.g. "5:0.33,8:0.33" -> [(5.0, 0.33), (8.0, 0.33)]: at +5% sell a third of the original
    position, at +8% sell another third, leaving a third to ride to the take-profit. Empty/off
    returns []. Fractions are of the position's qty AT ENTRY (init_qty), so "a third then a third"
    means exact thirds, not thirds-of-the-remainder.
    """
    raw = (os.environ.get("SCALE_OUT_TIERS", "") or "").strip()
    tiers: list[tuple[float, float]] = []
    for part in raw.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        g, f = part.split(":", 1)
        try:
            gain, frac = float(g), float(f)
        except ValueError:
            continue
        if gain > 0 and 0 < frac < 1:
            tiers.append((gain, frac))
    return sorted(tiers)


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
    # Atomic (temp + os.replace) so a concurrent reader (e.g. the sentinel) can't see a half-written
    # file, matching apply_decision.write_json_atomic.
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_PATH)


def load_live_state() -> dict:
    """LIVE mode: build a paper-state-shaped dict from broker truth so the rest of the screen/exit
    logic is unchanged. Cash + position qty/cost come from data/tick/broker_snapshot.json (the
    broker), while our stop/TP/entry_ts/scale metadata comes from data/live_state.json. We never
    write paper_state.json in live mode — live_execute.py owns live_state.json (incl. SOD equity)."""
    snap_path = DATA / "tick" / "broker_snapshot.json"
    live_path = DATA / "live_state.json"
    snap = json.loads(snap_path.read_text()) if snap_path.exists() else {}
    lstate = json.loads(live_path.read_text()) if live_path.exists() else {}
    lots = lstate.get("lots", {})

    # buying power — confirmed live shape: data.buying_power.buying_power (nested), fallback data.cash.
    # Tool results may arrive wrapped as {"data": {...}}; peel that first. Same mapping as
    # live_execute.parse_snapshot — keep the two in sync.
    def _flt(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    pf = snap.get("portfolio") or {}
    if isinstance(pf, dict) and isinstance(pf.get("data"), dict):
        pf = pf["data"]
    bp = pf.get("buying_power") if isinstance(pf, dict) else None
    cash = _flt(bp.get("buying_power")) if isinstance(bp, dict) else _flt(bp)
    if cash is None:
        cash = _flt(pf.get("cash")) or 0.0

    raw_pos = snap.get("positions")
    if isinstance(raw_pos, dict):
        raw_pos = raw_pos.get("data", raw_pos)
        if isinstance(raw_pos, dict):
            raw_pos = raw_pos.get("positions") or raw_pos.get("results") or []
    positions: dict[str, dict] = {}
    for p in raw_pos or []:
        if not isinstance(p, dict):
            continue
        sym = str(p.get("symbol") or p.get("ticker") or "").upper().strip()
        try:
            qty = float(p.get("quantity") or p.get("qty") or p.get("shares") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if not sym or qty <= 0:
            continue
        avg = p.get("average_buy_price") or p.get("average_price") or p.get("avg_cost") or p.get("price")
        try:
            entry = float(avg) if avg is not None else None
        except (TypeError, ValueError):
            entry = None
        lot = lots.get(sym, {})
        entry = lot.get("entry_price") or entry or 0.0
        positions[sym] = {
            "qty": qty, "entry_price": entry,
            "entry_ts": lot.get("entry_ts"),
            "stop_price": lot.get("stop_price"),
            "take_profit_price": lot.get("take_profit_price"),
            "high_water": lot.get("high_water"),  # trailing-stop peak (live_execute owns the writes)
            "init_qty": lot.get("init_qty", qty),
            "scaled": lot.get("scaled") or [],
            "stop_type": lot.get("stop_type", "synthetic"),
        }
    return {
        "cash": cash, "positions": positions, "realized_total": 0.0,
        "day": lstate.get("day"), "start_of_day_equity": lstate.get("start_of_day_equity"),
    }


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
        "daily_trend": a.get("daily_trend") or {"available": False},
        "avg_index_move_pct": a.get("avg_index_move_pct"),
        "vix_proxy_move_pct": a.get("vix_proxy_move_pct"),
        "source": rec.get("source"),
        "ts_et": rec.get("ts_et"),
    }


def build_context(now_utc: datetime | None = None, scope: str = "full") -> dict:
    """Gather one tick's full deterministic view — fresh quotes, equity-resolved caps, per-position
    P&L, the rule-based exit screen, and the GATE — and return it as the context dict. SHARED by the
    5-min planner (tick_context.main below) and the 1-min sentinel (sentinel.py), so both evaluate
    the identical exit rules and caps. The only side effect is the once-a-day start-of-day-equity
    rollover write to paper_state.json (paper mode); callers serialize that via data/.tick.lock.

    scope="full" (planner) fetches the whole universe — discovery movers + pins + held + armed — to
    screen for new entries. scope="monitor" (the 1-min sentinel) fetches ONLY held + armed + indexes:
    the sentinel just runs exits and armed-trigger checks, never screens new names, so quoting the
    discovery universe every minute is wasted load that pins us against Cboe's per-IP rate limit and
    starves the quotes that matter. Smaller, faster fetch => the gate stays fed every minute."""
    monitor = scope == "monitor"
    now_utc = now_utc or datetime.now(timezone.utc)
    now_et = now_utc.astimezone(mc.ET)
    today = now_et.strftime("%Y-%m-%d")
    session, is_open = mc.session_state(now_et)
    allow_offhours = env("ALLOW_OFFHOURS", "0") == "1"

    is_live = env("TRADING_MODE", "paper").strip().lower() == "live"
    state = load_live_state() if is_live else load_state()
    # Universe = dynamic discovery (today's top eligible movers) + always-watch pins + held.
    # Discovery replaces a static stock allowlist: a momentum engine has to see what's actually
    # moving market-wide, not a fixed watchlist. The pins (CANDIDATES) stay screened every tick and
    # are the fallback if discovery is disabled/down. See scripts/discover.py for the eligibility filter.
    # monitor scope skips BOTH the pins and discovery — the sentinel needs neither (no new-entry screen).
    pinned = [] if monitor else [s.strip().upper() for s in env(
        "CANDIDATES", "AAPL,NVDA,TSLA,AMD,MSFT,AMZN,META,GOOGL,F,PLTR").split(",") if s.strip()]
    discovered = []
    if not monitor and env("DISCOVERY_ENABLED", "1") == "1":
        try:
            import discover  # sibling; keyless Nasdaq screener + eligibility filter, cached per tick
            discovered = discover.discover()
        except Exception as e:  # discovery must NEVER break a tick — fall back to the pins
            sys.stderr.write(f"[tick] discovery failed, using pinned candidates only: {e}\n")
    # Armed entries (set by the planner, fired by the sentinel) must always get a fresh quote so the
    # sentinel can test their trigger — fold them into the universe even if discovery didn't surface them.
    armed_syms = list((state.get("armed_entries") or {}).keys())
    candidates = list(dict.fromkeys(discovered + pinned + armed_syms))  # ordered de-dupe (movers first)
    held = list(state["positions"].keys())
    # Index ETFs are ALWAYS fetched: SPY is the rel-strength benchmark and the market-data gate
    # needs index data — but they are excluded from entry candidates below (never momentum-buy beta).
    # Order MATTERS: the keyless quote source (Cboe) rate-limits a long per-symbol burst, dropping the
    # tail. So fetch the must-haves FIRST — indexes (data gate), then held + armed (exits + trigger
    # checks) — and only then discovery/pins. A clipped tail then loses a low-value mover, never an
    # index or an open position. (ordered de-dupe keeps first occurrence = highest priority.)
    symbols = list(dict.fromkeys(list(mc.INDEXES) + held + armed_syms + candidates))

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
        q = quotes.get(sym) or {}
        positions.append({
            "symbol": sym, "qty": round(qty, 6), "entry_price": entry,
            "last": lp, "value": round(val, 2), "pnl_usd": round(pnl, 2), "pnl_pct": pnl_pct,
            "stop_price": p.get("stop_price"), "take_profit_price": p.get("take_profit_price"),
            "entry_ts": p.get("entry_ts"),  # for the max-hold time-exit
            "init_qty": p.get("init_qty", round(qty, 6)),  # scale-out base (qty at entry); fallback for pre-existing positions
            "scaled": p.get("scaled") or [],               # scale-out tiers already taken (gain%s)
            "stop_type": p.get("stop_type", "synthetic"),  # synthetic = engine-tick only, not a resting broker stop
            # OG DD + intraday structure for the Tier-1 hold-risk monitor (hold_risk.py):
            "conviction": p.get("conviction"), "hold_intent": p.get("hold_intent"),
            "thesis_type": p.get("thesis_type"),
            "range_pos": mc.range_position(q), "intraday_pct": mc.intraday_pct(q),
        })

    equity = round(state["cash"] + pos_value, 2)

    # --- day rollover + day P&L for the circuit breaker ---
    # In LIVE mode live_execute.py owns start-of-day equity (in live_state.json); tick_context must
    # NOT write paper_state.json. We still need a SOD baseline for the breaker cap this tick — use
    # live_state's value if present, else fall back to current equity (live_execute re-checks the
    # breaker authoritatively against broker numbers before any entry).
    if state.get("day") != today or state.get("start_of_day_equity") is None:
        state["day"] = today
        state["start_of_day_equity"] = equity
        if not is_live:
            # Persist ONLY the day + SOD fields, under .state.lock, via a fresh re-read — never the
            # snapshot loaded before the (slow) quote fetch, which would clobber any exit the sentinel
            # wrote in between. The rollover fires once a day, so the lock is essentially never contended.
            from state_lock import state_lock
            with state_lock():
                fresh = load_state()
                if fresh.get("day") != today or fresh.get("start_of_day_equity") is None:
                    fresh["day"] = today
                    fresh["start_of_day_equity"] = equity
                    save_state(fresh)
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

    # Sizing caps are a FRACTION OF LIVE EQUITY, resolved to dollars against THIS tick's equity so
    # they auto-scale as the account compounds or draws down (no stale hard-$ ceiling frozen at the
    # funding balance). MAX_POSITION_PCT is the SINGLE per-name concentration cap — it replaced the
    # old MAX_SYMBOL_WEIGHT, which was the same formula (symbol_value / equity) once sizing is a %.
    # Downstream (decide.py headroom, apply_decision checks) consumes the derived *_USD values, so we
    # expose both the configured pct and the resolved dollars.
    max_pos_pct = envf("MAX_POSITION_PCT", 0.10)
    max_exp_pct = envf("MAX_TOTAL_EXPOSURE_PCT", 0.80)
    # Daily-loss circuit breaker: a fraction of START-OF-DAY equity, but never more than a hard $
    # ceiling. The % lets the breaker scale with the account; the cap bounds absolute daily pain once
    # the book is large (at 5% / $500 the cap overtakes the % at $10k equity). Anchored to
    # start-of-day equity (not live) so the day's threshold is fixed, not shrinking intraday as P&L drops.
    sod_equity = state.get("start_of_day_equity") or equity
    daily_pct = envf("DAILY_MAX_LOSS_PCT", 0.05)
    daily_cap = envf("DAILY_MAX_LOSS_CAP_USD", 500.0)
    daily_max_loss = round(min(daily_pct * sod_equity, daily_cap), 2)
    # Per-trade loss budget: a fraction of LIVE equity (matches the sizing caps). At a 4% stop this
    # implies a max notional of 25x the budget = 25% of equity, comfortably above MAX_POSITION_PCT, so
    # it stays a slack backstop at every account size (only bites if STOP_LOSS_PCT is widened).
    per_trade_pct = envf("MAX_PER_TRADE_LOSS_PCT", 0.01)
    caps = {
        "MAX_POSITION_PCT": max_pos_pct,
        "MAX_TOTAL_EXPOSURE_PCT": max_exp_pct,
        "MAX_POSITION_USD": round(max_pos_pct * equity, 2),        # per-name ceiling, % of live equity
        "MAX_TOTAL_EXPOSURE_USD": round(max_exp_pct * equity, 2),  # total invested ceiling, % of equity
        "MAX_OPEN_POSITIONS": int(envf("MAX_OPEN_POSITIONS", 10)),
        "STOP_LOSS_PCT": envf("STOP_LOSS_PCT", 4.0),
        "TAKE_PROFIT_PCT": envf("TAKE_PROFIT_PCT", 4.0),
        # Trailing stop (live path): 0 = OFF (static entry-based stop). >0 trails this % below the
        # high-water mark, ratchet-only, beginning once a lot is up TRAIL_ACTIVATE_PCT. See live_execute.
        "TRAIL_STOP_PCT": envf("TRAIL_STOP_PCT", 0.0),
        "TRAIL_ACTIVATE_PCT": envf("TRAIL_ACTIVATE_PCT", 0.0),
        "TRAIL_MIN_STEP_PCT": envf("TRAIL_MIN_STEP_PCT", 0.5),
        "SIGNAL_THRESHOLD_PCT": envf("SIGNAL_THRESHOLD_PCT", 2.0),  # DEPRECATED (old intraday-pop trigger)
        "GAP_THRESHOLD_PCT": envf("GAP_THRESHOLD_PCT", 7.0),        # catalyst entry: min overnight gap %
        "VOL_MULT_MIN": envf("VOL_MULT_MIN", 2.0),                  # catalyst entry: min volume vs 20d avg
        "MAX_HOLD_DAYS": envf("MAX_HOLD_DAYS", 0.0),                # multi-day time-exit (drift window)
        "DAILY_MAX_LOSS_PCT": daily_pct,
        "DAILY_MAX_LOSS_CAP_USD": daily_cap,
        "DAILY_MAX_LOSS_USD": daily_max_loss,                      # = min(pct * start-of-day equity, cap)
        "MAX_PER_TRADE_LOSS_PCT": per_trade_pct,
        "MAX_PER_TRADE_LOSS_USD": round(per_trade_pct * equity, 2),  # = pct * live equity (slack backstop)
        "MIN_POSITION_USD": envf("MIN_POSITION_USD", 0.0),  # 0 = no floor; >0 rejects dust fills
        # --- order execution model (marketable limits + slippage + hybrid stops) ---
        "SLIPPAGE_BPS": envf("SLIPPAGE_BPS", 10.0),          # adverse bps applied to every paper fill
        "MARKETABLE_LIMIT_PCT": envf("MARKETABLE_LIMIT_PCT", 0.5),  # buy limit cap above the touch
        "PREFER_WHOLE_SHARES": 1 if str(os.environ.get("PREFER_WHOLE_SHARES", "1")).strip().lower()
        not in ("0", "false", "no", "") else 0,             # floor buys to whole shares -> resting-stop eligible
    }
    regime = latest_regime()

    # --- DETERMINISTIC screen (rules, no LLM): exits + entry candidates ---
    # Exits are pure risk rules — never a model decision. Stop/TP first, then time-based exits.
    tp, sl = caps["TAKE_PROFIT_PCT"], caps["STOP_LOSS_PCT"]
    # FREE-REIN trader (owner mandate 2026-06-05): NO mechanical entry signal — the agent picks.
    # The deterministic layer only gathers the day's tradable movers + enforces the risk seatbelts
    # (stops, caps, daily breaker); Stage-2 (the agent) decides what's worth a shot and for how long.
    # Screen / risk-mgmt tuning knobs (all .env-overridable; v1 defaults hold MULTI-DAY, no EOD flatten).
    cooldown_min = envf("COOLDOWN_MIN", 1440.0)        # no re-entry within N min of an exit (anti-whipsaw; 1d default)
    flatten_min = envf("FLATTEN_BEFORE_CLOSE_MIN", 0.0)  # 0 = HOLD OVERNIGHT (the drift edge is overnight)
    winddown_min = envf("WINDDOWN_BEFORE_CLOSE_MIN", 0.0)  # in the last N min, lock GREEN positions early (0 = off)
    winddown_profit = envf("WINDDOWN_MIN_PROFIT_PCT", 1.0)  # only wind down positions with pnl% >= this (0 = any green)
    no_entry_last_min = envf("NO_ENTRY_LAST_MIN", 0.0)   # 0 = entering near the close is fine for a multi-day hold
    max_hold_min = envf("MAX_HOLD_MIN", 0.0)           # 0 = OFF (no intraday recycle; v1 holds the drift out)
    max_hold_days = caps["MAX_HOLD_DAYS"]              # time-exit after N calendar days (~drift window) — the core exit
    stall_band_pct = envf("STALL_BAND_PCT", 2.0)       # max |pnl%| for max-hold-MIN to fire (only if MAX_HOLD_MIN>0)
    tiers = scale_out_tiers()                          # partial profit-take ladder (gain% -> fraction of entry qty); [] = off
    # Tier-1 hold-risk monitor (hold_risk.py): a cheap per-tick protective SELL of a DETERIORATING
    # loser — tighter than the hard stop, gated so it doesn't whipsaw a noisy-but-fine position. The
    # hard STOP_LOSS_PCT stop stays the backstop under it. HOLD_RISK_SELL=0 disables the auto-sell
    # (positions are still scored, for logging + the Tier-2 manage cadence).
    hold_risk_sell = env("HOLD_RISK_SELL", "1") == "1"
    soft_cut = envf("SOFT_CUT_PCT", 4.0)               # protective-sell a falling loser at this %
    # Risk-adaptive re-DD cadence (minutes) by band -> drives the Tier-2 manage-DD timing (decide.py):
    # riskier holdings get re-checked sooner; a calm winner coasts.
    redd_ttl = {"low": envf("HOLD_REDD_TTL_LOW_MIN", 60.0), "medium": envf("HOLD_REDD_TTL_MED_MIN", 20.0),
                "high": envf("HOLD_REDD_TTL_HIGH_MIN", 5.0), "critical": envf("HOLD_REDD_TTL_CRIT_MIN", 0.0)}
    # Minutes until the 16:00 ET close (None when not in a regular session).
    close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    mins_to_close = (close_et - now_et).total_seconds() / 60.0 if is_open else None

    exits = []
    for p in positions:
        lp, pp = p.get("last"), p.get("pnl_pct")
        sp, tpp = p.get("stop_price"), p.get("take_profit_price")
        if lp is None:
            continue  # need a fresh quote to (synthetically) sell against
        # Tier-1 hold-risk score (cheap, deterministic) — surfaced on the position for logging + the
        # Tier-2 manage cadence, and used below for the protective soft-cut.
        prisk = hold_risk.score(p, now_utc, soft_cut_pct=soft_cut, redd_ttl=redd_ttl)
        p["risk"] = prisk
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
        # Tier-1 SMART soft-cut: bail a deteriorating loser before the hard stop (risk monitor's call).
        elif hold_risk_sell and prisk.get("protective_sell"):
            reason = f"risk-exit: {prisk.get('sell_reason')}"
        # Time-based exits (price-independent risk mgmt; bound the synthetic-stop gap window).
        elif is_open and flatten_min > 0 and mins_to_close is not None and mins_to_close <= flatten_min:
            reason = f"EOD flatten ({int(mins_to_close)}m to close)"
        elif (is_open and winddown_min > 0 and mins_to_close is not None
              and mins_to_close <= winddown_min and pp is not None and pp >= winddown_profit):
            # EOD wind-down: in the last N min, lock in GREEN positions early rather than risk the
            # gain eroding into a choppy close. Asymmetric on purpose — losers are NOT touched here;
            # they keep their full runway to the hard flatten (which takes everything regardless).
            reason = f"EOD wind-down: +{pp}% locked ({int(mins_to_close)}m to close)"
        elif max_hold_min > 0 and p.get("entry_ts"):
            # Recycle STALLED capital: only force-exit on the time limit when the position is
            # going nowhere (|pnl| within the stall band). A name still climbing toward TP (or
            # bleeding toward the stop) is left for its price rule. stall_band <= 0 = blind time stop.
            try:
                age_min = (now_utc - datetime.fromisoformat(p["entry_ts"])).total_seconds() / 60.0
                stalled = stall_band_pct <= 0 or (pp is not None and abs(pp) < stall_band_pct)
                if age_min >= max_hold_min and stalled:
                    band = "" if stall_band_pct <= 0 else f", stalled |{pp}%|<{stall_band_pct:g}%"
                    reason = f"max-hold {int(age_min)}m >= {int(max_hold_min)}m{band}"
            except (ValueError, TypeError):
                pass
        elif max_hold_days > 0 and p.get("entry_ts"):
            # CATALYST-DRIFT v1 core exit: the multi-day drift window elapsed -> time-exit the hold.
            # Unlike max-hold-MIN this fires regardless of pnl (the drift is realized by N days, win or lose).
            try:
                age_days = (now_utc - datetime.fromisoformat(p["entry_ts"])).days
                if age_days >= max_hold_days:
                    reason = f"max-hold {age_days}d >= {int(max_hold_days)}d (drift window elapsed)"
            except (ValueError, TypeError):
                pass
        if reason:
            exits.append({"symbol": p["symbol"], "reason": reason})
        elif tiers and pp is not None and pp > 0:
            # Scale-out ladder: no full-exit rule fired, so check the partial profit-take tiers.
            # Sell a fraction of the ENTRY qty for every tier the gain has cleared but we haven't
            # taken yet (collapsing a multi-tier gap-up into one slice so we don't miss a tier that
            # reverses before the next 5-min tick). The position stays open; apply_decision marks the
            # tiers taken and ratchets the stop to breakeven after the first trim.
            already = p.get("scaled") or []
            base = p.get("init_qty") or p.get("qty") or 0.0
            due = [(g, f) for (g, f) in tiers if pp >= g and g not in already]
            qty_out = round(base * sum(f for _, f in due), 6)
            if due and qty_out > 0:
                gains = [g for g, _ in due]
                pct = int(round(sum(f for _, f in due) * 100))
                tier_lbl = ",".join(f"+{g:g}%" for g in gains)
                exits.append({"symbol": p["symbol"],
                              "reason": f"scale-out {pct}% at +{pp}% (tier {tier_lbl})",
                              "qty": qty_out, "scale_tiers": gains})

    # Entry candidates (FREE-REIN): the day's tradable movers, handed to the agent with NO mechanical
    # signal gate — not held, not in post-exit cooldown, with a fresh same-day quote, in a non-hostile
    # regime. Ranked by intraday move (liveliest first) purely as ordering. Stage 2 (the agent) has
    # full discretion: it picks what's worth a shot (momentum / breakout / news / its own read) and the
    # hold horizon (scalp or ride). The agent IS the screen.
    # FREE-REIN: only a CONFIRMED downtrend (risk_off) blocks entries — the seatbelt against buying into
    # a falling market. Elevated volatility no longer blocks (more vol = more action); agent sizes down.
    hostile = (regime.get("posture") == "risk_off")
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
    armed_set = set((state.get("armed_entries") or {}).keys())  # already have a pending trigger -> don't re-DD
    entry_candidates = []
    if allow_entries and not hostile:
        movers = []
        for c in cand:
            sym = c["symbol"]
            if sym in non_tradable or sym in held or sym in cooling or sym in armed_set:
                continue
            q = quotes.get(sym) or {}
            if q.get("last") is None:
                continue
            if c.get("date") and c["date"] != today:
                continue  # this symbol's own quote is stale — fail closed, skip it
            movers.append(c)                        # FREE REIN: no signal gate — the agent decides
        movers.sort(key=lambda c: -(c.get("intraday_pct") or 0))   # liveliest first (ordering only)
        entry_candidates = [{"symbol": c["symbol"], "intraday_pct": c.get("intraday_pct"),
                             "range_pos": c.get("range_pos"), "last": c.get("last"),
                             "reason": (f"{c.get('intraday_pct')}% intraday, {regime.get('posture')} "
                                        "regime — agent's discretion (free rein)")}
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

    return context


def main() -> int:
    """Planner entry point: build the shared context, write the audit snapshot + the compact LLM
    packet, and print the GATE line the wrapper branches on."""
    context = build_context()
    regime = context["regime"]
    caps = context["caps"]

    # Compact packet for the LLM (only what it needs to decide).
    packet = {
        "mode": context["mode"],
        "allow_entries": context["allow_entries"],   # false => exits/HOLD only; no new positions
        "data_stale": context["data_stale"],
        "regime": {k: regime.get(k) for k in
                   ("posture", "volatility_regime", "breadth_regime", "daily_trend", "session")},
        "portfolio": context["portfolio"],
        "positions": [{k: p[k] for k in ("symbol", "qty", "entry_price", "last", "pnl_pct")}
                      for p in context["positions"]],
        "candidates": [c for c in context["candidates"] if c["last"] is not None],
        "caps": caps,
    }

    TICK.mkdir(parents=True, exist_ok=True)
    (TICK / "context_latest.json").write_text(json.dumps(context, indent=2))
    (TICK / "packet_latest.json").write_text(json.dumps(packet))

    print(f"GATE={context['gate']}" + (f":{context['gate_reason']}" if context['gate_reason'] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
