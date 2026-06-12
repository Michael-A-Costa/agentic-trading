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

PAPER mode only. The live counterpart is live_tick_context.py, which calls
build_context(state=load_live_state(), mode="live") and shares this file's logic.

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


def scale_out_tiers(env_key: str = "SCALE_OUT_TIERS") -> list[tuple[float, float]]:
    """Parse a tier ladder env var ("gain%:fracOfInitQty, ...") into sorted (gain, frac) pairs.

    e.g. "5:0.33,8:0.33" -> [(5.0, 0.33), (8.0, 0.33)]: at +5% sell a third of the original
    position, at +8% sell another third, leaving a third to ride to the take-profit. Empty/off
    returns []. Fractions are of the position's qty AT ENTRY (init_qty), so "a third then a third"
    means exact thirds, not thirds-of-the-remainder. env_key selects the ladder: the global
    SCALE_OUT_TIERS, or the per-book DISCO_SCALE_OUT_TIERS overlay (two-book v2.1).
    """
    raw = (os.environ.get(env_key, "") or "").strip()
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


def _build_caps(equity: float, sod_equity: float) -> dict:
    """Resolve all .env cap fractions to dollars for one tick's equity snapshot."""
    max_pos_pct = envf("MAX_POSITION_PCT", 0.10)
    max_exp_pct = envf("MAX_TOTAL_EXPOSURE_PCT", 0.80)
    daily_pct = envf("DAILY_MAX_LOSS_PCT", 0.05)
    daily_cap = envf("DAILY_MAX_LOSS_CAP_USD", 500.0)
    per_trade_pct = envf("MAX_PER_TRADE_LOSS_PCT", 0.01)
    return {
        "MAX_POSITION_PCT": max_pos_pct,
        "MAX_TOTAL_EXPOSURE_PCT": max_exp_pct,
        "MAX_POSITION_USD": round(max_pos_pct * equity, 2),
        "MAX_TOTAL_EXPOSURE_USD": round(max_exp_pct * equity, 2),
        "MAX_OPEN_POSITIONS": int(envf("MAX_OPEN_POSITIONS", 10)),
        "STOP_LOSS_PCT": envf("STOP_LOSS_PCT", 4.0),
        "TAKE_PROFIT_PCT": envf("TAKE_PROFIT_PCT", 4.0),
        # Per-book exit overlay (two-book v2.1): disco lots take profit at a TIGHTER level than the
        # pead let-run TP. 0/unset = off (disco uses the global TAKE_PROFIT_PCT). Backtested
        # 2026-06-10 (playbook §6e/6f + exit-strategy-findings doc): on the movers entry disco
        # actually trades, a +10-15% full exit beats let-run on win%/median/give-back at zero mean
        # cost, and dominates at the account level at every slot count tested (4-30).
        "DISCO_TAKE_PROFIT_PCT": envf("DISCO_TAKE_PROFIT_PCT", 0.0),
        # Live arming gate for the overlay: paper applies DISCO_TAKE_PROFIT_PCT immediately (it IS
        # the validation bed); live ignores it until this flag is 1 (flip after >=30 paper disco
        # round-trips per the two-book disarm rule).
        "DISCO_EXITS_LIVE": 1 if str(os.environ.get("DISCO_EXITS_LIVE", "0")).strip().lower()
                            not in ("0", "false", "no", "") else 0,
        # Per-book trail overlay (findings A7/A9, 2026-06-11): once the moonshot-remnant ladder
        # (DISCO_SCALE_OUT_TIERS) trims a disco lot, the remnant rides a TIGHTER trail than pead's
        # let-run rungs — width calibrated on the VELO 6/11 tape (5% survives the shakeout; the
        # MOV-M harness puts tr5@10 at the robustness peak across both portfolio accountings).
        # 0/unset = disco uses the global TRAIL_* rungs.
        "DISCO_TRAIL_STOP_PCT": envf("DISCO_TRAIL_STOP_PCT", 0.0),
        "DISCO_TRAIL_ACTIVATE_PCT": envf("DISCO_TRAIL_ACTIVATE_PCT", 0.0),
        "TRAIL_STOP_PCT": envf("TRAIL_STOP_PCT", 0.0),
        "TRAIL_ACTIVATE_PCT": envf("TRAIL_ACTIVATE_PCT", 0.0),
        "TRAIL_BREAKEVEN_AT_PCT": envf("TRAIL_BREAKEVEN_AT_PCT", 0.0),
        "TRAIL_MIN_STEP_PCT": envf("TRAIL_MIN_STEP_PCT", 0.5),
        "SIGNAL_THRESHOLD_PCT": envf("SIGNAL_THRESHOLD_PCT", 2.0),
        "GAP_THRESHOLD_PCT": envf("GAP_THRESHOLD_PCT", 7.0),
        "VOL_MULT_MIN": envf("VOL_MULT_MIN", 2.0),
        "MAX_HOLD_DAYS": envf("MAX_HOLD_DAYS", 0.0),
        "DAILY_MAX_LOSS_PCT": daily_pct,
        "DAILY_MAX_LOSS_CAP_USD": daily_cap,
        "DAILY_MAX_LOSS_USD": round(min(daily_pct * sod_equity, daily_cap), 2),
        "MAX_PER_TRADE_LOSS_PCT": per_trade_pct,
        "MAX_PER_TRADE_LOSS_USD": round(per_trade_pct * equity, 2),
        "MIN_POSITION_USD": envf("MIN_POSITION_USD", 0.0),
        "SLIPPAGE_BPS": envf("SLIPPAGE_BPS", 10.0),
        "MARKETABLE_LIMIT_PCT": envf("MARKETABLE_LIMIT_PCT", 0.5),
        "PREFER_WHOLE_SHARES": 1 if str(os.environ.get("PREFER_WHOLE_SHARES", "1")).strip().lower()
                               not in ("0", "false", "no", "") else 0,
    }


def build_context(now_utc: datetime | None = None, scope: str = "full", *,
                  state: dict | None = None, mode: str = "paper") -> dict:
    """Gather one tick's full deterministic view — fresh quotes, equity-resolved caps, per-position
    P&L, the rule-based exit screen, and the GATE — and return it as the context dict. SHARED by the
    5-min planner (tick_context.main / live_tick_context.main) and the 1-min sentinel (sentinel.py),
    so both evaluate identical exit rules and caps.

    state: pre-loaded portfolio dict. When None (default), loads paper_state.json — the paper path.
           Live callers pass load_live_state() from live_tick_context.py.
    mode:  "paper" | "live" — written into context["mode"] (the safety label read by the executors).
           Paper mode also persists the once-a-day SOD rollover to paper_state.json.

    scope="full" (planner) fetches the whole universe — discovery movers + pins + held + armed.
    scope="monitor" (the 1-min sentinel) fetches ONLY held + armed + indexes: the sentinel runs
    exits and armed-trigger checks only, so the lean fetch avoids bursting Cboe's per-IP limit."""
    monitor = scope == "monitor"
    now_utc = now_utc or datetime.now(timezone.utc)
    now_et = now_utc.astimezone(mc.ET)
    today = now_et.strftime("%Y-%m-%d")
    session, is_open = mc.session_state(now_et)
    allow_offhours = env("ALLOW_OFFHOURS", "0") == "1"

    if state is None:
        state = load_state()

    # Fast path: market closed and off-hours disabled → skip discovery + quote fetches entirely.
    # session_state() is pure time math; we already know the gate will be SKIP.
    if not monitor and not is_open and not allow_offhours:
        pos_value = sum(p["qty"] * p["entry_price"] for p in state["positions"].values())
        equity = round(state["cash"] + pos_value, 2)
        sod = state.get("start_of_day_equity") or equity
        day_pnl = round(equity - sod, 2)
        positions = [
            {"symbol": s, "qty": p["qty"], "entry_price": p["entry_price"],
             "last": None, "value": round(p["qty"] * p["entry_price"], 2),
             "pnl_usd": 0.0, "pnl_pct": None,
             "stop_price": p.get("stop_price"), "take_profit_price": p.get("take_profit_price"),
             "entry_ts": p.get("entry_ts"), "init_qty": p.get("init_qty", p["qty"]),
             "scaled": p.get("scaled") or [], "stop_type": p.get("stop_type", "synthetic"),
             "conviction": p.get("conviction"), "hold_intent": p.get("hold_intent"),
             "thesis_type": p.get("thesis_type"), "range_pos": None, "intraday_pct": None}
            for s, p in state["positions"].items()
        ]
        return {
            "ts_utc": now_utc.isoformat(timespec="seconds"),
            "ts_et": now_et.isoformat(timespec="seconds"),
            "mode": mode, "session": session,
            "market_open": False, "allow_offhours": False, "allow_entries": False,
            "data_stale": False, "stale_reason": "market_not_open",
            "quote_source": None,
            "regime": latest_regime(),
            "portfolio": {
                "cash": round(state["cash"], 2),
                "positions_value": round(pos_value, 2),
                "equity": equity,
                "start_of_day_equity": sod,
                "day_pnl": day_pnl,
                "realized_total": round(state.get("realized_total", 0.0), 2),
                "open_positions": len(state["positions"]),
            },
            "positions": positions,
            "candidates": [],
            "screen": {"exits": [], "entry_candidates": [], "hostile_regime": False, "cooling": []},
            "caps": _build_caps(equity, sod),
            "gate": "SKIP",
            "gate_reason": f"market_{session}",
        }

    # Universe = dynamic discovery (today's top movers) + held + armed entries.
    # Pure momentum: only trade what the market is already moving. No fixed watchlist —
    # a pinned name on a quiet day is not a momentum trade. CANDIDATES is kept solely as a
    # last-resort fallback when discovery fails entirely (rate-limit, network error); it is
    # never mixed into an otherwise-healthy discovery result.
    # monitor scope skips discovery — the sentinel needs neither (no new-entry screen).
    fallback_pins = [] if monitor else [s.strip().upper() for s in env(
        "CANDIDATES", "").split(",") if s.strip()]
    discovered = []
    if not monitor and env("DISCOVERY_ENABLED", "1") == "1":
        try:
            import discover  # sibling; keyless Nasdaq screener + eligibility filter, cached per tick
            discovered = discover.discover()
        except Exception as e:
            sys.stderr.write(f"[tick] discovery failed, using fallback pins: {e}\n")
    # Use fallback pins ONLY when discovery produced nothing (failed or returned empty).
    pins = fallback_pins if not discovered else []
    # PEAD discovery: stocks that reported earnings in the last PEAD_LOOKBACK_DAYS days.
    # These are prepended so the DD agent sees them as prioritized catalyst candidates.
    # Controlled by PEAD_DISCOVERY_ENABLED (default 1). Cached per ET trading day.
    pead_meta: dict[str, dict] = {}
    if not monitor and env("PEAD_DISCOVERY_ENABLED", "1") == "1":
        try:
            import discover_pead  # sibling; Nasdaq earnings calendar, cached per day
            pead_meta = discover_pead.pead_meta()
        except Exception as e:
            sys.stderr.write(f"[tick] pead discovery failed: {e}\n")
    # Market caps for the two-book router (v2 plan): the PEAD calendar rows and the gainer-screen
    # detail cache are the only keyless mktcap sources. Names in neither map stay unknown — the
    # router fails them to the disco book (never into the evidence cohort on a guess).
    mktcap_by_sym: dict[str, float] = {}
    try:
        _dcache = json.loads((TICK / "discovery_latest.json").read_text())
        for _d in _dcache.get("detail") or []:
            if _d.get("symbol") and _d.get("mktcap") is not None:
                mktcap_by_sym[str(_d["symbol"]).upper()] = _d["mktcap"]
    except (OSError, ValueError):
        pass
    for _s, _m in pead_meta.items():
        if _m.get("mktcap") is not None:
            mktcap_by_sym.setdefault(_s, _m["mktcap"])
    # Armed entries must always get a fresh quote so the sentinel can test their trigger.
    armed_syms = list((state.get("armed_entries") or {}).keys())
    # PEAD candidates lead (freshest catalyst signal), then momentum movers, then pins/fallback.
    candidates = list(dict.fromkeys(list(pead_meta) + discovered + pins + armed_syms))
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
    # The quote fetch is the dominant cost of this step; log the universe size so a slow tick is
    # attributable (the runner otherwise shows nothing between "[3/5]…" and the GATE line).
    sys.stderr.write(f"[tick] fetching quotes for {len(symbols)} symbols "
                     f"({len(mc.INDEXES)} idx + {len(held)} held + {len(armed_syms)} armed + "
                     f"{len(candidates)} cand)\n")
    sys.stderr.flush()
    try:
        quotes, source = mc.fetch_quotes(symbols)
    except (OSError, RuntimeError) as e:
        fetch_error = str(e)

    # Persist the quotes this tick already fetched so the parallel DD probes can reuse them instead of
    # each re-hitting Cboe (N cold processes bursting it is what trips the 429 -> no_live_quote). Only
    # the FULL-scope planner tick writes it (the monitor sentinel's partial set must not clobber the
    # candidate quotes a DD will look up); dd_probe falls back to a live fetch if it's stale/missing.
    if not monitor and quotes:
        try:
            TICK.mkdir(parents=True, exist_ok=True)
            tmp = (TICK / "quotes_latest.json").with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"ts": now_utc.timestamp(), "source": source, "quotes": quotes}))
            os.replace(tmp, TICK / "quotes_latest.json")
        except OSError:
            pass  # quote cache is a best-effort optimization; dd_probe fetches live if it's absent

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
            "book": p.get("book") or "disco",   # two-book split: lot ownership (v2 plan)
            "range_pos": mc.range_position(q), "intraday_pct": mc.intraday_pct(q),
        })

    equity = round(state["cash"] + pos_value, 2)

    # --- day rollover + day P&L for the circuit breaker ---
    if state.get("day") != today or state.get("start_of_day_equity") is None:
        state["day"] = today
        state["start_of_day_equity"] = equity
        if mode == "paper":
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
        pm = pead_meta.get(sym)
        entry: dict = {
            "symbol": sym, "last": q.get("last"),
            "intraday_pct": mc.intraday_pct(q) if q else None,
            "range_pos": mc.range_position(q) if q else None,
            "date": q.get("date"),  # per-symbol freshness (fail-closed: stale candidate is excluded)
        }
        if pm:
            entry["catalyst"] = "earnings"
            entry["earnings_date"] = pm["earnings_date"]
            entry["earnings_time"] = pm["time"].replace("time-", "")
            entry["days_since_earnings"] = pm["days_since"]
        if mktcap_by_sym.get(sym) is not None:
            entry["mktcap"] = mktcap_by_sym[sym]   # two-book router input (pead = mega-cap only)
        cand.append(entry)

    sod_equity = state.get("start_of_day_equity") or equity
    caps = _build_caps(equity, sod_equity)
    regime = latest_regime()

    # --- DETERMINISTIC screen (rules, no LLM): exits + entry candidates ---
    # Exits are pure risk rules — never a model decision. Stop/TP first, then time-based exits.
    tp, sl = caps["TAKE_PROFIT_PCT"], caps["STOP_LOSS_PCT"]
    # Per-book TP overlay for the fallback %-rule below (lots normally carry an explicit
    # take_profit_price set at entry, already book-aware; this covers legacy lots without one).
    # Paper applies the disco override immediately; live only once DISCO_EXITS_LIVE=1.
    disco_tp = caps.get("DISCO_TAKE_PROFIT_PCT", 0.0) or 0.0
    disco_exits_on = mode != "live" or bool(caps.get("DISCO_EXITS_LIVE"))
    disco_tp_on = disco_tp > 0 and disco_exits_on
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
    # Per-book ladder (findings A7/A9): disco lots harvest most of the position at the tier and let
    # the remnant ride (the moonshot ticket). Same live-arming gate as the disco TP overlay.
    disco_tiers = scale_out_tiers("DISCO_SCALE_OUT_TIERS") if disco_exits_on else []
    # Tier-1 hold-risk monitor (hold_risk.py): a cheap per-tick protective SELL of a DETERIORATING
    # loser — tighter than the hard stop, gated so it doesn't whipsaw a noisy-but-fine position. The
    # hard STOP_LOSS_PCT stop stays the backstop under it. HOLD_RISK_SELL=0 disables the auto-sell
    # (positions are still scored, for logging + the Tier-2 manage cadence).
    hold_risk_sell = env("HOLD_RISK_SELL", "1") == "1"
    soft_cut = envf("SOFT_CUT_PCT", 8.0)               # protective-sell a falling loser at this %
    # critical-band auto-sell OFF by default: backtest_exit_policy (2026-06-09) — crit65 fails to
    # beat the plain stop on mean+sharpe; the deep soft-cut is the only Tier-1 sell that earns it.
    crit_sell = env("HOLD_RISK_CRIT_SELL", "0") == "1"
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
        prisk = hold_risk.score(p, now_utc, soft_cut_pct=soft_cut, redd_ttl=redd_ttl,
                                crit_sell=crit_sell)
        p["risk"] = prisk
        reason = None
        eff_tp = disco_tp if (disco_tp_on and str(p.get("book") or "disco") == "disco") else tp
        # Prefer the explicit per-position stop/TP levels set at buy; fall back to the % rule.
        # "synthetic stop" = enforced here at tick time, NOT a resting broker order (no gap cover).
        if sp is not None and lp <= sp:
            reason = f"synthetic stop hit: {lp} <= stop {sp} ({pp}%)"
        elif tpp is not None and lp >= tpp:
            reason = f"take-profit hit: {lp} >= {tpp} ({pp}%)"
        elif sp is None and pp is not None and pp <= -sl:
            reason = f"stop-loss {pp}% <= -{sl}%"
        elif tpp is None and pp is not None and pp >= eff_tp:
            reason = f"take-profit {pp}% >= {eff_tp}%"
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
        # Per-book ladder selection: a disco lot uses the DISCO_SCALE_OUT_TIERS overlay when set
        # (the A7/A9 moonshot-remnant harvest); everything else keeps the global ladder.
        eff_tiers = (disco_tiers if (disco_tiers and str(p.get("book") or "disco") == "disco")
                     else tiers)
        if reason:
            exits.append({"symbol": p["symbol"], "reason": reason})
        elif eff_tiers and pp is not None and pp > 0:
            # Scale-out ladder: no full-exit rule fired, so check the partial profit-take tiers.
            # Sell a fraction of the ENTRY qty for every tier the gain has cleared but we haven't
            # taken yet (collapsing a multi-tier gap-up into one slice so we don't miss a tier that
            # reverses before the next 5-min tick). The position stays open; apply_decision marks the
            # tiers taken and ratchets the stop to breakeven after the first trim.
            already = p.get("scaled") or []
            base = p.get("init_qty") or p.get("qty") or 0.0
            due = [(g, f) for (g, f) in eff_tiers if pp >= g and g not in already]
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
    # Regime gate, SPLIT by evidence (2026-06-09 regime-split backtest; docs/remediation-plan P-gate):
    # PEAD entries during confirmed SPY downtrends kept their full mean edge (+1.74%/trade LARGE vs
    # +1.49% benign, n=56) — a blanket downtrend blackout was discarding ~16% of historical signals.
    # But those trades are noisier (median -0.9%, win 48% vs 55%) and free-rein pop-chasing into a
    # falling tape is the trap the override existed to close. So:
    #   acute stress (<=1 index green AND VIX proxy +3%) -> ALL entries off (rare, same-day pause)
    #   confirmed downtrend (not acute)                  -> PEAD-ONLY mode: only earnings-window
    #       candidates pass the screen; decide.py suppresses any commit whose measured gap+vol
    #       signal (pead_qualified) is not True and sizes the survivors down one tier (x0.6).
    posture = regime.get("posture")
    _green, _total = regime.get("breadth_green"), regime.get("breadth_total")
    _vix = regime.get("vix_proxy_move_pct")
    # REGIME_ENTRY_GATE=0 lifts the regime-based entry gate entirely (owner override): no acute
    # entries-off, no downtrend PEAD-only suppression — entries are free-rein in ANY posture. The HARD
    # safety layer (caps, stops, daily-loss breaker, tripwire) is unaffected; only the regime read is.
    regime_gate = os.environ.get("REGIME_ENTRY_GATE", "1").strip().lower() not in ("0", "false", "no", "")
    acute = bool(regime_gate and posture == "risk_off" and _total and _green is not None and _green <= 1
                 and _vix is not None and _vix > 3)
    downtrend_pead_only = bool(regime_gate and posture == "risk_off" and not acute)
    hostile = acute
    held = {p["symbol"] for p in positions}
    # A quote is only a LIVE signal during regular hours AND when it carries today's date. Don't hang
    # the whole entry gate on ONE symbol: a lone flaky Cboe fetch for SPY (seen 2026-06-09: SPY came
    # back null while QQQ/IWM/DIA were all fresh) would otherwise mark data stale and kill entries for
    # the tick. Use the NEWEST date across the index proxies — entries gate only when EVERY proxy is
    # missing/stale (a real data outage), not when a single one flakes.
    ref_dates = [(quotes.get(s) or {}).get("date") for s in ("SPY", "QQQ", "IWM", "DIA")]
    ref_date = max((d for d in ref_dates if d), default=None)
    data_stale = (ref_date != today) if ref_date else True
    near_close = bool(no_entry_last_min > 0 and mins_to_close is not None
                      and mins_to_close <= no_entry_last_min)
    allow_entries = bool(is_open and not data_stale and not near_close)
    stale_reason = ("market_not_open" if not is_open
                    else f"stale_quote_date={ref_date}" if data_stale
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
    pead_neg_dropped: list[str] = []
    pead_direction = env("PEAD_DIRECTION", "up").strip().lower()
    discovered_set = {str(s).upper() for s in discovered}
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
            # PEAD direction pre-filter (v2 plan Phase 0): a long-only cash account can never
            # trade a negative earnings gap, so a fresh (day-0/1) reporter whose gap is DOWN is
            # dropped BEFORE it consumes a DD slot. Only the fresh-gap window is testable from
            # today's quote (open vs prev close; intraday move as fallback); older reporters pass
            # (their gap day already screened them). A name the gainer screen surfaced on its own
            # merits keeps its slot — it's a mover regardless of the earnings gap's sign.
            if (pead_direction == "up" and c.get("catalyst") == "earnings"
                    and (c.get("days_since_earnings") if c.get("days_since_earnings") is not None else 99) <= 1
                    and sym not in discovered_set):
                _o, _pc = q.get("open"), q.get("prev_day_close")
                _move = ((_o / _pc - 1) * 100 if (_o and _pc) else c.get("intraday_pct"))
                if _move is not None and _move < 0:
                    pead_neg_dropped.append(sym)
                    continue
            # Whole-share-only: skip names where even 1 share exceeds the per-name cap.
            # Buying exactly 1 share is the executor's fallback when the conviction budget
            # is short of a share; if that 1 share itself exceeds the cap, it can never fill.
            max_pos = caps.get("MAX_POSITION_USD", 0.0)
            if max_pos > 0 and (q.get("last") or 0.0) > max_pos:
                continue
            # Downtrend = PEAD-only: free-rein movers don't trade into a confirmed falling market
            # (the override's original trap); earnings-window names keep their measured edge.
            if downtrend_pead_only and c.get("catalyst") != "earnings":
                continue
            movers.append(c)                        # FREE REIN (benign tape): no signal gate — the agent decides
        # PEAD candidates sort first (day 0 most urgent), then by intraday magnitude.
        movers.sort(key=lambda c: (
            1 if c.get("catalyst") == "earnings" else 2,   # PEAD leads
            -(c.get("intraday_pct") or 0),
        ))
        def _candidate_reason(c: dict) -> str:
            if c.get("catalyst") == "earnings":
                day_label = f"day +{c['days_since_earnings']}"
                return (f"earnings {c['earnings_date']} ({c.get('earnings_time','?')}, {day_label}"
                        f" of drift window) — gap-drift candidate")
            return (f"{c.get('intraday_pct')}% intraday, {regime.get('posture')} "
                    "regime — agent's discretion (free rein)")
        # (downtrend_pead_only: non-earnings movers were filtered above, so every candidate here
        #  is in its drift window; decide.py still verifies the measured gap+vol signal.)
        entry_candidates = [{"symbol": c["symbol"], "intraday_pct": c.get("intraday_pct"),
                             "range_pos": c.get("range_pos"), "last": c.get("last"),
                             **({"mktcap": c["mktcap"]} if c.get("mktcap") is not None else {}),
                             **({"catalyst": c["catalyst"],
                                 "earnings_date": c["earnings_date"],
                                 "earnings_time": c.get("earnings_time"),
                                 "days_since_earnings": c.get("days_since_earnings")}
                                if c.get("catalyst") else {}),
                             "reason": _candidate_reason(c)}
                            for c in movers]
    screen = {"exits": exits, "entry_candidates": entry_candidates,
              "hostile_regime": hostile, "downtrend_pead_only": downtrend_pead_only,
              "cooling": sorted(cooling),
              **({"pead_negative_gap_dropped": pead_neg_dropped} if pead_neg_dropped else {})}

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
        "mode": mode,
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
            # SETTLED spendable cash (live: broker buying_power, excludes unsettled T+1 proceeds).
            # Paper has no settling, so full cash is spendable — fall back to it. The entry gate sizes
            # headroom off this, NOT NAV cash, so DD is skipped when there's nothing to actually deploy.
            "settled_buying_power": round(state.get("buying_power", state["cash"]), 2),
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


def main(state: dict | None = None, mode: str = "paper") -> int:
    """Planner entry point: build the shared context, write the audit snapshot + the compact LLM
    packet, and print the GATE line the wrapper branches on.

    state/mode are passed by live_tick_context.main(); paper callers use the defaults."""
    context = build_context(state=state, mode=mode)
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
    # Atomic (temp + os.replace) so a concurrent reader — the sentinel, or the open-DD sweep
    # capturing entry_candidates — can't see a half-written file if a tick rewrites it mid-read.
    for name, blob in (("context_latest.json", json.dumps(context, indent=2)),
                       ("packet_latest.json", json.dumps(packet))):
        tmp = (TICK / name).with_suffix(".json.tmp")
        tmp.write_text(blob)
        os.replace(tmp, TICK / name)

    print(f"GATE={context['gate']}" + (f":{context['gate_reason']}" if context['gate_reason'] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
