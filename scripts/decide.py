#!/usr/bin/env python3
"""
decide.py — two-stage decision orchestrator for one tick. Produces the final action list; the
deterministic executor (apply_decision.py) still re-checks caps and logs.

  Stage 1  (DETERMINISTIC, no LLM — computed in tick_context.py): screen the packet -> {exits, entry_candidates}
  Stage 2  (only if there are entry_candidates AND allow_entries): for each shortlisted name,
           dd_probe.py gathers deep quant DD, then DD_MODEL (default Sonnet) + web news/catalyst
           search decides commit/reject + size. Default is REJECT.

Exits execute immediately (mechanical risk mgmt). Entries only after Stage-2 commits. Output:
data/tick/decision_latest.json = {actions, rationale, screen, dd}. The LLMs only judge; all data
gathering is scripts.
"""
from __future__ import annotations

import concurrent.futures
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")            # cache freshness is measured in ET calendar days

import stock_memory as memory                # sibling: long-term per-symbol eval memory + exclusions
import catalyst_log                          # sibling: forward filter-lift ledger (gap event -> verdict)
from apply_decision import extract_decision  # sibling: robust JSON extraction

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
TICK = REPO / "data" / "tick"
DD_CACHE = REPO / "data" / "dd_cache.json"
MANAGE_CACHE = REPO / "data" / "manage_cache.json"   # Tier-2: last-managed ts + verdict per holding
PYEXE = sys.executable or "python3"


def load_cache() -> dict:
    try:
        return json.loads(DD_CACHE.read_text())
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict) -> None:
    DD_CACHE.parent.mkdir(parents=True, exist_ok=True)
    DD_CACHE.write_text(json.dumps(cache, indent=2))


def load_manage_cache() -> dict:
    try:
        return json.loads(MANAGE_CACHE.read_text())
    except (OSError, ValueError):
        return {}


def save_manage_cache(cache: dict) -> None:
    MANAGE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    MANAGE_CACHE.write_text(json.dumps(cache, indent=2))


def claude_bin() -> str:
    return (os.environ.get("AGENTIC_CLAUDE") or shutil.which("claude")
            or str(Path.home() / ".local/bin/claude"))


# --- Per-tick token accounting ----------------------------------------------------------------
# Every model call in a tick funnels through run_claude (entry DDs, manage DDs, and the live
# MCP relay), and those calls run in a ThreadPoolExecutor. Each call appends its usage to this
# ledger under a lock, so usage_summary() at end-of-tick is an exact per-tick token/cost total.
_USAGE_LOCK = threading.Lock()
_USAGE_LEDGER: list[dict] = []


def reset_usage() -> None:
    with _USAGE_LOCK:
        _USAGE_LEDGER.clear()


def _record_usage(label: str, model: str, usage: dict | None, cost_usd: float | None,
                  elapsed_s: float, error: str | None = None) -> None:
    """Thread-safe append of one headless-Claude call's token + cost figures."""
    rec = {"label": label, "model": model, "elapsed_s": round(elapsed_s, 1)}
    if error:
        rec["error"] = error
    if isinstance(usage, dict):
        for k in ("input_tokens", "output_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens"):
            rec[k] = usage.get(k)
    if cost_usd is not None:
        rec["cost_usd"] = cost_usd
    with _USAGE_LOCK:
        _USAGE_LEDGER.append(rec)


def usage_summary() -> dict:
    """Roll the tick's headless-Claude calls into a token/cost total + per-call breakdown."""
    with _USAGE_LOCK:
        calls = list(_USAGE_LEDGER)

    def _sum(key: str) -> int:
        return sum(int(c.get(key) or 0) for c in calls)

    inp, out = _sum("input_tokens"), _sum("output_tokens")
    cc, cr = _sum("cache_creation_input_tokens"), _sum("cache_read_input_tokens")
    return {
        "n_calls": len(calls),
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
        "total_tokens": inp + out + cc + cr,    # all billed tokens (cached input is billed too)
        "cost_usd": round(sum(float(c.get("cost_usd") or 0.0) for c in calls), 4),
        "calls": calls,
    }


def _parse_claude_json(stdout: str) -> tuple[str | None, dict | None, float | None]:
    """Pull (result_text, usage, total_cost_usd) out of `claude --output-format json`. Falls back
    to treating stdout as raw text with no usage if it isn't the expected JSON envelope."""
    try:
        obj = json.loads(stdout)
    except (ValueError, TypeError):
        return (stdout or None), None, None
    if not isinstance(obj, dict):
        return (stdout or None), None, None
    return obj.get("result"), obj.get("usage"), obj.get("total_cost_usd")


def run_claude(prompt: str, model: str, tools: list | None = None, mcp: bool = False,
               timeout: int = 360, label: str = "claude") -> str | None:
    """Headless Claude. --strict-mcp-config => only the servers we pass (none, or the RH MCP).

    Returns the model's result text on success, or None on timeout / non-zero exit / empty output —
    so the caller can tell a model FAILURE (retry next tick, don't cache) apart from a real 'reject'
    verdict. Uses --output-format json so each call's token usage + cost are captured into the tick
    ledger (see usage_summary()); `label` attributes the spend (e.g. "dd:AAPL", "manage:MSFT").
    """
    cmd = [claude_bin(), "-p", prompt, "--model", model, "--output-format", "json",
           "--strict-mcp-config"]
    if mcp:
        cmd += ["--mcp-config", str(REPO / ".mcp.json")]
    if tools:
        cmd += ["--allowedTools", *tools, "--dangerously-skip-permissions"]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        sys.stderr.write(f"[decide] claude invocation failed: {e}\n")
        _record_usage(label, model, None, None, time.time() - t0, error=str(e)[:120])
        return None
    if r.returncode != 0:
        sys.stderr.write(f"[decide] claude exit {r.returncode}: {(r.stderr or '')[:300]}\n")
        _record_usage(label, model, None, None, time.time() - t0, error=f"exit_{r.returncode}")
        return None
    text, usage, cost = _parse_claude_json(r.stdout)
    _record_usage(label, model, usage, cost, time.time() - t0)
    return text if (text and text.strip()) else None


# Stage-2 DD agent toolset: quant is script-gathered; the agent adds live quote + news research.
DD_TOOLS = ["WebSearch", "WebFetch", "mcp__robinhood-trading__get_equity_quotes"]


def fmt_candidate(c: dict) -> str:
    """One screened entry candidate -> compact signal string for the log."""
    return f"{c.get('symbol', '?')}(+{c.get('intraday_pct')}% intraday)"


def fmt_dd_line(d: dict) -> str:
    """One Stage-2 DD verdict -> a human line explaining WHY it committed / rejected / errored.

    Shows the verdict, whether it came from a fresh model call or the TTL cache (and how old),
    the size on a commit, and the model's own reason — so a reject is never a silent count.
    """
    sym = d.get("symbol", "?")
    dec = (d.get("decision") or "?").upper()
    if d.get("cached"):
        src = f"cached {d.get('cached_age_min', '?')}m"   # reused verdict — explains a fast tick
    elif d.get("dd_elapsed_s") is not None:
        src = f"fresh {d['dd_elapsed_s']}s"                # live Sonnet+web call — explains a slow tick
    else:
        src = "fresh"
    reason = (d.get("reason") or d.get("error") or "(no reason given)").strip().replace("\n", " ")
    size = f" ${d.get('dollar_amount')} {d.get('conviction')}" if dec == "COMMIT" else ""
    return f"  DD {sym}: {dec} [{src}]{size} — {reason[:300]}"


def run_dd(c: dict, regime: dict, caps: dict, portfolio: dict, dd_model: str) -> dict:
    """Stage-2 research agent for one symbol: deep quant probe + multi-tool news/catalyst commit.

    `decision` is one of commit / reject / error. 'error' (model timeout / non-zero exit /
    unparseable output) is NOT a verdict — callers must not buy on it and must not cache it.
    """
    sym = str(c.get("symbol", "")).upper().strip()
    try:
        subprocess.run([PYEXE, str(SCRIPTS / "dd_probe.py"), sym],
                       capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as e:
        sys.stderr.write(f"[decide] dd_probe {sym} failed ({e}); using any existing probe file\n")
    dd_file = TICK / f"dd_{sym}.json"
    try:
        dd = json.loads(dd_file.read_text()) if dd_file.exists() else {"symbol": sym, "error": "no_dd"}
    except (OSError, ValueError):
        dd = {"symbol": sym, "error": "dd_unreadable"}

    # Headroom = how much we could ACTUALLY add now (cap, remaining exposure, and cash), so the
    # model sizes within what the deterministic gate will accept instead of proposing a number
    # that silently rejects.
    headroom = max(0.0, min(caps["MAX_POSITION_USD"],
                            caps["MAX_TOTAL_EXPOSURE_USD"] - portfolio.get("exposure", 0.0),
                            portfolio.get("cash", 0.0)))
    dd_input = json.dumps({
        "symbol": sym,
        "screen_reason": c.get("reason", ""),
        "screen_signal": {"intraday_pct": c.get("intraday_pct"),  # move from today's open
                          "range_pos": c.get("range_pos"),       # near 1.0 = at the day's high
                          "last": c.get("last")},                # the screen had no signal gate — you pick
        "regime": regime,
        "sizing": {"MAX_POSITION_USD": caps["MAX_POSITION_USD"],
                   "MIN_POSITION_USD": caps.get("MIN_POSITION_USD", 0.0),
                   "available_headroom_usd": round(headroom, 2),
                   "available_cash": round(portfolio.get("cash", 0.0), 2),
                   "stop_loss_pct": caps["STOP_LOSS_PCT"],
                   "take_profit_pct": caps["TAKE_PROFIT_PCT"],
                   "max_per_trade_loss_usd": caps.get("MAX_PER_TRADE_LOSS_USD")},
        "portfolio": {"open_positions": portfolio.get("open_positions", 0),
                      "max_open_positions": caps["MAX_OPEN_POSITIONS"],
                      "held_symbols": portfolio.get("held", [])},
        # Prime with our OWN past verdicts on this name (None if never evaluated): if the same
        # disqualifier still holds, the model rejects fast; if the picture changed, it says so.
        "prior_evaluation": memory.get_note(sym),
        "dd": dd,
    })
    # Per-DD wall-clock cap. Kept tight (default 240s) so that — even when several DDs run in
    # parallel — one slow web-research call can't push the tick past the ~5-min launchd cadence.
    dd_timeout = int(os.environ.get("DD_CLAUDE_TIMEOUT_S", "240"))
    out = run_claude((SCRIPTS / "dd_prompt.txt").read_text() + dd_input,
                     dd_model, tools=DD_TOOLS, mcp=True, timeout=dd_timeout, label=f"dd:{sym}")
    if out is None:
        return {"symbol": sym, "decision": "error", "error": "dd_model_failed", "conviction": None,
                "dollar_amount": None, "reason": "", "catalysts": [], "risks": []}
    commit = extract_decision(out)
    if commit.get("_parse_error"):
        return {"symbol": sym, "decision": "error", "error": "dd_parse_error", "conviction": None,
                "dollar_amount": None, "reason": "model output unparseable", "catalysts": [],
                "risks": [], "raw_excerpt": out.strip()[:300]}
    decision = (commit.get("decision") or "reject").lower()
    dollar = commit.get("dollar_amount")
    # Commit invariant: a commit MUST carry a positive, finite dollar_amount — otherwise it would
    # silently evaporate at the buy step. Downgrade such a "commit" to a logged reject.
    if decision == "commit":
        try:
            d = float(dollar)
            if not math.isfinite(d) or d <= 0:
                raise ValueError
        except (TypeError, ValueError):
            decision, dollar = "reject", None
            commit["reason"] = f"commit returned without a valid size; {commit.get('reason', '')}".strip()
    return {"symbol": sym, "decision": decision, "conviction": commit.get("conviction"),
            "dollar_amount": dollar, "reason": commit.get("reason", ""),
            "catalyst_type": commit.get("thesis_type") or commit.get("catalyst_type"),  # the agent's thesis tag
            "hold_intent": commit.get("hold_intent"),            # scalp | swing | runner — agent's horizon call
            "entry_trigger": commit.get("entry_trigger"),        # optional {price,direction} -> sentinel-armed entry
            "catalysts": commit.get("catalysts", []), "risks": commit.get("risks", []),
            "next_earnings_date": commit.get("next_earnings_date"),
            "never_buy": bool(commit.get("never_buy")),          # structural disqualifier -> exclude
            "never_buy_reason": commit.get("never_buy_reason")}


MANAGE_TOOLS = ["WebSearch", "WebFetch", "mcp__robinhood-trading__get_equity_quotes"]


def run_manage_dd(p: dict, regime: dict, caps: dict, portfolio: dict, dd_model: str) -> dict:
    """Tier-2: re-assess ONE held position on FRESH data + news. Returns
    {action: keep|trim|exit|add, trim_fraction, add_dollars, conviction, hold_intent, reason}.
    Defaults to KEEP on any failure — the hard stop + Tier-1 monitor still protect the downside."""
    sym = str(p.get("symbol", "")).upper().strip()
    try:
        subprocess.run([PYEXE, str(SCRIPTS / "dd_probe.py"), sym],
                       capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as e:
        sys.stderr.write(f"[manage] dd_probe {sym} failed ({e})\n")
    dd_file = TICK / f"dd_{sym}.json"
    try:
        dd = json.loads(dd_file.read_text()) if dd_file.exists() else {"symbol": sym, "error": "no_dd"}
    except (OSError, ValueError):
        dd = {"symbol": sym, "error": "dd_unreadable"}

    cur_val = float(p.get("value") or 0.0)
    headroom = max(0.0, min(caps["MAX_POSITION_USD"] - cur_val,
                            caps["MAX_TOTAL_EXPOSURE_USD"] - portfolio.get("exposure", 0.0),
                            portfolio.get("cash", 0.0)))
    age_h = None
    if p.get("entry_ts"):
        try:
            age_h = round((datetime.now(timezone.utc)
                           - datetime.fromisoformat(p["entry_ts"])).total_seconds() / 3600.0, 1)
        except (ValueError, TypeError):
            age_h = None
    risk = p.get("risk") or {}
    manage_input = json.dumps({
        "symbol": sym,
        "position": {"entry_price": p.get("entry_price"), "qty": p.get("qty"),
                     "current_value_usd": round(cur_val, 2), "last": p.get("last"),
                     "pnl_pct": p.get("pnl_pct"), "age_hours": age_h,
                     "og_conviction": p.get("conviction"), "og_hold_intent": p.get("hold_intent"),
                     "og_thesis": p.get("thesis_type")},
        "risk": {"score": risk.get("risk"), "band": risk.get("band"), "reasons": risk.get("reasons")},
        "sizing": {"MAX_POSITION_USD": caps["MAX_POSITION_USD"],
                   "available_headroom_usd": round(headroom, 2),
                   "available_cash": round(portfolio.get("cash", 0.0), 2)},
        "regime": regime,
        "prior_evaluation": memory.get_note(sym),
        "dd": dd,
    })
    dd_timeout = int(os.environ.get("DD_CLAUDE_TIMEOUT_S", "240"))
    out = run_claude((SCRIPTS / "dd_manage_prompt.txt").read_text() + manage_input,
                     dd_model, tools=MANAGE_TOOLS, mcp=True, timeout=dd_timeout, label=f"manage:{sym}")
    if out is None:
        return {"symbol": sym, "action": "keep", "error": "manage_model_failed",
                "reason": "manage model failed -> keep (stop + Tier-1 still protect)"}
    v = extract_decision(out)
    if v.get("_parse_error"):
        return {"symbol": sym, "action": "keep", "error": "manage_parse_error",
                "reason": "unparseable manage output -> keep"}
    action = (v.get("action") or "keep").lower()
    if action not in ("keep", "trim", "exit", "add"):
        action = "keep"
    return {"symbol": sym, "action": action, "trim_fraction": v.get("trim_fraction"),
            "add_dollars": v.get("add_dollars"), "conviction": v.get("conviction"),
            "hold_intent": v.get("hold_intent"), "reason": v.get("reason", ""),
            "risks": v.get("risks", [])}


def main() -> int:
    reset_usage()   # start this tick's token ledger clean
    context = json.loads((TICK / "context_latest.json").read_text())
    caps = context["caps"]
    regime = context.get("regime", {})
    dd_model = os.environ.get("DD_MODEL", "claude-sonnet-4-6")
    max_dd = int(os.environ.get("MAX_DD_CANDIDATES", "2"))

    # --- Stage 1 is now DETERMINISTIC (computed in tick_context.py) — just read it ---
    screen = context.get("screen", {})
    exits = screen.get("exits") or []
    candidates = screen.get("entry_candidates") or []

    # Carry the optional partial-sell fields (qty + scale_tiers) through for scale-out exits; a
    # full close has neither and defaults to selling the whole position in apply_decision.
    actions = [{"symbol": e.get("symbol"), "side": "sell", "reason": e.get("reason", ""),
                **({"qty": e["qty"]} if e.get("qty") is not None else {}),
                **({"scale_tiers": e["scale_tiers"]} if e.get("scale_tiers") else {})}
               for e in exits if e.get("symbol")]

    # --- Stage 2: deep DD + commit, with a per-symbol ONCE-A-DAY cache (re-DD only on a real move) ---
    # A DD is a ~85s Sonnet+web-research call — the tick's dominant token/latency cost. Dynamic
    # discovery keeps surfacing the SAME top movers every 5-min tick, so a short rolling TTL re-burned
    # that call many times a day on names whose thesis hadn't changed. Policy: one fresh DD per name
    # per ET calendar day (commit AND reject), reused all day, UNLESS the setup materially changes.
    # The invalidation trigger is ASYMMETRIC by verdict, because the two verdicts go stale differently:
    #   * COMMIT  -> re-DD on a move in EITHER direction past DD_CACHE_DRIFT_PCT. A committed bullish
    #               thesis is broken by a drop AND blown by a run-up (entering late = chasing).
    #   * REJECT  -> re-DD only on an UPSIDE breakout (price up past DD_REJECT_REDD_PCT, or range_pos
    #               pushed into a fresh intraday-high zone). There is no independent news feed in this
    #               repo, so the "fresh catalyst" that should overturn a reject is read from price/range
    #               action — the market revealing it, which is exactly what this gap-drift strategy
    #               trades. A downside continuation only CONFIRMS the reject, so we don't re-burn on it.
    # Execution still re-checks fresh price + caps + allow_entries in apply_decision, so reusing a
    # day-old commit never trades on a stale price. Errors are never cached (retry next tick).
    # DD_CACHE_TTL_MIN (default 0 = day-scoped) adds an optional sub-day rolling cap; set any trigger
    # %% to 0 to disable it.
    dd_results = []
    cache = load_cache()
    today_et = datetime.now(ET).strftime("%Y-%m-%d")   # cache keyed to the ET trading day
    rolling_ttl = int(os.environ.get("DD_CACHE_TTL_MIN", "0")) * 60   # 0 = no sub-day cap (once/day)
    # A cached COMMIT is re-DD'd mid-day if the live price has moved more than this % (either way)
    # since the DD — the "significant price movement" that invalidates a same-day entry thesis.
    drift_pct = float(os.environ.get("DD_CACHE_DRIFT_PCT", "3.0"))
    # A cached REJECT is overturned only by an UPSIDE breakout since the verdict (no news feed exists,
    # so price/range action is the catalyst proxy): live price up past DD_REJECT_REDD_PCT, OR range_pos
    # pushed into a fresh intraday-high zone (>= DD_REJECT_REDD_RANGE and higher than at reject time).
    reject_redd_pct = float(os.environ.get("DD_REJECT_REDD_PCT", "2.0"))
    reject_redd_range = float(os.environ.get("DD_REJECT_REDD_RANGE", "0.90"))
    now = time.time()
    cache_dirty = False
    # Prune dead entries so the file doesn't grow every session: verdicts stamped for a prior ET day,
    # plus legacy entries (no "day" stamp) past the 24h fallback. Both can no longer ever be a cache
    # hit, so dropping them is loss-free and keeps each tick's read+rewrite of the blob small.
    stale_keys = [k for k, e in cache.items()
                  if (e.get("day") not in (None, today_et))
                  or (e.get("day") is None and now - e.get("ts", 0) >= 86400)]
    for k in stale_keys:
        cache.pop(k, None)
    cache_dirty = bool(stale_keys)
    book_full = False
    entry_did_fresh = False   # did the entry pass run any FRESH (uncached) DDs this tick? (gates the manage wave)
    headroom = None
    book_full_note = ""   # set inside the entries block (where portfolio is in scope) if full
    if context.get("allow_entries") and candidates:
        positions_ctx = context.get("positions", [])
        portfolio = {
            "cash": context["portfolio"]["cash"],
            "exposure": context["portfolio"].get("positions_value", 0.0),
            "open_positions": context["portfolio"].get("open_positions", len(positions_ctx)),
            "held": [p["symbol"] for p in positions_ctx],
        }
        # Portfolio-level short-circuit: when the book is full, available headroom (mirrors run_dd's
        # formula) drops below a usable lot and EVERY entry rejects on that single portfolio-wide
        # constraint — nothing to do with the name. Skip Stage-2 entirely so we don't burn N ~85s
        # Sonnet+web DD calls per tick re-rejecting the same top movers; log one "book full" line.
        # Threshold defaults to MIN_POSITION_USD (or $25 if unset); tune via MIN_ENTRY_HEADROOM_USD.
        headroom = max(0.0, min(caps["MAX_POSITION_USD"],
                                caps["MAX_TOTAL_EXPOSURE_USD"] - portfolio["exposure"],
                                portfolio["cash"]))
        min_headroom = float(os.environ.get("MIN_ENTRY_HEADROOM_USD",
                                            caps.get("MIN_POSITION_USD") or 25.0))
        book_full = headroom < min_headroom
        if book_full:
            book_full_note = (f"${headroom:.2f} headroom (exposure {portfolio['exposure']:.0f}"
                              f"/{caps['MAX_TOTAL_EXPOSURE_USD']:.0f}, "
                              f"{portfolio['open_positions']} pos) < usable lot")

        shortlist = []
        if not book_full:
            for c in candidates[:max_dd]:
                sym = str(c.get("symbol", "")).upper().strip()
                if sym:
                    shortlist.append((sym, c))

        # Serve cached verdicts synchronously: reuse a commit OR reject DD'd EARLIER TODAY (same ET
        # day), unless price has drifted past DD_CACHE_DRIFT_PCT since the verdict. Errors are never
        # cached so they retry. Cache misses need a fresh Stage-2 call and go to the parallel pool.
        cache_hits = {}
        fresh_jobs = []
        for sym, c in shortlist:
            res = None
            cached = cache.get(sym)
            if cached:
                cdec = (cached.get("result") or {}).get("decision")
                age = now - cached.get("ts", 0)
                # Same-day reuse (fall back to ts age for pre-existing entries without a "day" stamp);
                # an optional rolling_ttl can further cap reuse to sub-day if set.
                cday = cached.get("day")
                same_day = (cday == today_et) if cday else (age < 86400)
                within_rolling = rolling_ttl <= 0 or age < rolling_ttl
                # Asymmetric re-DD trigger (see policy comment above): commit = move either way;
                # reject = upside breakout only (price OR range_pos). move_pct is signed: + is up.
                ref, cur = cached.get("ref_price"), c.get("last")
                move_pct = (cur / ref - 1) * 100 if (ref and cur) else 0.0
                if cdec == "commit":
                    stale = drift_pct > 0 and abs(move_pct) > drift_pct
                else:  # reject — overturn only on an upside breakout, never on downside follow-through
                    ref_rp, cur_rp = cached.get("ref_range_pos"), c.get("range_pos")
                    broke_up = reject_redd_pct > 0 and move_pct > reject_redd_pct
                    broke_range = bool(ref_rp is not None and cur_rp is not None
                                       and cur_rp >= reject_redd_range and cur_rp > ref_rp)
                    stale = broke_up or broke_range
                if cdec in ("commit", "reject") and same_day and within_rolling and not stale:
                    res = {**cached["result"], "cached": True, "cached_age_min": int(age / 60)}
            if res is not None:
                cache_hits[sym] = res
            else:
                fresh_jobs.append((sym, c))
        entry_did_fresh = bool(fresh_jobs)   # entries did real DD work this tick -> defer manage to a quieter tick

        # Run the cache-miss DDs CONCURRENTLY. Each run_dd is subprocess-bound (dd_probe + a headless
        # `claude` web-research call), so it releases the GIL and threads give true parallelism.
        # Serial DD was the cause of ticks overrunning the 5-min launchd cadence — 2-3 fresh DDs cost
        # the SUM of their web-research calls; in parallel the tick's wall-clock is the SLOWEST single
        # DD instead. Cache + long-term-memory writes stay on the main thread (after the pool drains)
        # to avoid races.
        fresh_results = {}
        if fresh_jobs:
            def timed_dd(cand):
                t0 = time.time()
                r = run_dd(cand, regime, caps, portfolio, dd_model)
                return {**r, "dd_elapsed_s": round(time.time() - t0, 1)}
            workers = min(len(fresh_jobs), int(os.environ.get("DD_MAX_PARALLEL", "4")))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(timed_dd, c): sym for sym, c in fresh_jobs}
                for fut in concurrent.futures.as_completed(futs):
                    sym = futs[fut]
                    try:
                        fresh_results[sym] = fut.result()
                    except Exception as e:  # one DD blowing up must not sink the whole tick
                        fresh_results[sym] = {"symbol": sym, "decision": "error",
                                              "error": f"dd_exception: {e}", "conviction": None,
                                              "dollar_amount": None, "reason": "", "catalysts": [],
                                              "risks": []}

        # Reassemble in screen order: cache + persist fresh commits/rejects, then build buy actions.
        for sym, c in shortlist:
            res = cache_hits.get(sym) or fresh_results.get(sym)
            if res is None:
                continue
            # Cache commits and rejects (each reused under its asymmetric trigger at read time); never
            # cache an error — a transient model/timeout failure must be retried, not frozen.
            if not res.get("cached") and res.get("decision") in ("commit", "reject"):
                cache[sym] = {"ts": now, "day": today_et,          # ET day -> once-a-day reuse
                              "ref_price": c.get("last"),           # price at DD time (drift trigger)
                              "ref_range_pos": c.get("range_pos"),  # range pos at DD time (reject breakout trigger)
                              "result": {k: v for k, v in res.items() if k != "dd_elapsed_s"}}
                cache_dirty = True
                # Persist the main points to long-term memory and auto-exclude a never-buy name.
                memory.record(sym, decision=res["decision"], conviction=res.get("conviction"),
                              reason=res.get("reason", ""), catalysts=res.get("catalysts"),
                              risks=res.get("risks"), next_earnings_date=res.get("next_earnings_date"),
                              never_buy=res.get("never_buy"), never_buy_reason=res.get("never_buy_reason"))
            dd_results.append(res)
            if res.get("decision") == "commit" and res.get("dollar_amount"):
                tag = f"DD/{res.get('conviction', '?')}" + ("/cached" if res.get("cached") else "")
                act = {"symbol": sym, "side": "buy",
                       "dollar_amount": res["dollar_amount"],
                       "conviction": res.get("conviction"),     # OG DD -> persisted on the position
                       "hold_intent": res.get("hold_intent"),   #   so the Tier-1 risk monitor can reason
                       "thesis_type": res.get("catalyst_type"),
                       "reason": f"[{tag}] {res.get('reason', '')}"}
                # Optional level-triggered entry: if the DD returned an entry_trigger, ARM it for the
                # sentinel to fire on the cross instead of buying now (LLM arms, fast loop fires). No
                # trigger -> immediate buy (default, unchanged). ENTRY_ARMING=0 forces immediate.
                trig = res.get("entry_trigger")
                arming_on = os.environ.get("ENTRY_ARMING", "1").strip().lower() not in ("0", "false", "no", "")
                if (arming_on and isinstance(trig, dict) and trig.get("price")
                        and str(trig.get("direction")).lower() in ("above", "below")):
                    act["arm"] = True
                    act["entry_trigger"] = {"price": trig["price"],
                                            "direction": str(trig["direction"]).lower()}
                actions.append(act)
        if cache_dirty:
            save_cache(cache)

    # --- Tier-2: risk-adaptive manage-DD on HELD positions that are DUE (riskiest first, capped) ---
    # Each holding carries a risk-adaptive re-DD TTL (hold_risk.py: critical->now, high->5m, med->20m,
    # low->60m). A holding is DUE when that TTL has elapsed since its last manage-DD (or it's critical,
    # or never managed). We re-check only the due ones, on FRESH news, and keep/trim/exit/add. The hard
    # stop + the Tier-1 monitor cover the gaps between reviews. Best-effort: never sink the tick.
    manage_results = []
    try:
        positions_ctx = context.get("positions", [])
        # Interleave the two DD waves: only run the manage wave on a tick that did NOT already run fresh
        # entry DDs, so a single tick never pays for BOTH waves (~2x). Tier-1 protective sells run every
        # tick regardless (tick_context), so a deferred manage-DD never leaves a critical holding unwatched.
        if (context.get("market_open") and not context.get("data_stale") and positions_ctx
                and not entry_did_fresh):
            mcache = load_manage_cache()
            exiting = {a.get("symbol") for a in actions if a.get("side") == "sell"}  # already exiting this tick
            due = []
            for p in positions_ctx:
                sym = p.get("symbol")
                if not sym or sym in exiting:
                    continue
                prisk = p.get("risk") or {}
                ttl_min = float(prisk.get("redd_ttl_min", 20.0))
                ent = mcache.get(sym)
                is_due = (ent is None or prisk.get("band") == "critical"
                          or (now - float(ent.get("ts", 0))) >= ttl_min * 60)
                if is_due:
                    due.append((float(prisk.get("risk", 0) or 0), p))
            due.sort(key=lambda x: -x[0])                       # riskiest holdings first
            max_manage = int(os.environ.get("HOLD_REVIEW_MAX_PER_TICK", "4"))
            due_ps = [p for _, p in due[:max_manage]]
            if due_ps:
                pm = {"cash": context["portfolio"]["cash"],
                      "exposure": context["portfolio"].get("positions_value", 0.0)}
                mfresh = {}
                workers = min(len(due_ps), int(os.environ.get("DD_MAX_PARALLEL", "4")))
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = {ex.submit(run_manage_dd, p, regime, caps, pm, dd_model): p["symbol"]
                            for p in due_ps}
                    for fut in concurrent.futures.as_completed(futs):
                        s = futs[fut]
                        try:
                            mfresh[s] = fut.result()
                        except Exception as e:               # one manage-DD blowing up isn't fatal
                            mfresh[s] = {"symbol": s, "action": "keep",
                                         "error": f"manage_exception: {e}", "reason": ""}
                for p in due_ps:
                    sym = p["symbol"]
                    res = mfresh.get(sym)
                    if not res:
                        continue
                    prisk = p.get("risk") or {}
                    mcache[sym] = {"ts": now, "band": prisk.get("band"),
                                   "ttl_min": prisk.get("redd_ttl_min"), "result": res}
                    manage_results.append(res)
                    act = res.get("action")
                    if act == "exit":
                        actions.append({"symbol": sym, "side": "sell",
                                        "reason": f"[manage/exit] {res.get('reason', '')}"})
                    elif act == "trim" and res.get("trim_fraction"):
                        try:
                            frac = max(0.0, min(1.0, float(res["trim_fraction"])))
                        except (TypeError, ValueError):
                            frac = 0.0
                        qty = round(frac * float(p.get("qty") or 0.0), 6)
                        if qty > 0:
                            actions.append({"symbol": sym, "side": "sell", "qty": qty,
                                            "reason": f"[manage/trim {int(frac * 100)}%] {res.get('reason', '')}"})
                    elif act == "add" and res.get("add_dollars"):
                        try:
                            addd = float(res["add_dollars"])
                        except (TypeError, ValueError):
                            addd = 0.0
                        if addd > 0:
                            actions.append({"symbol": sym, "side": "buy", "dollar_amount": addd,
                                            "conviction": res.get("conviction"),
                                            "hold_intent": res.get("hold_intent"),
                                            "thesis_type": p.get("thesis_type"),
                                            "reason": f"[manage/add] {res.get('reason', '')}"})
                    # "keep" -> no action (the cache ts is still bumped so it coasts for its TTL)
                save_manage_cache(mcache)
    except Exception as e:  # a manage-pass failure must not sink the tick's entries/exits
        sys.stderr.write(f"[decide] manage pass failed (ignored): {e}\n")

    # Forward filter-lift ledger: record each agent-evaluated gap candidate's verdict (commit/reject)
    # so catalyst_filter_report.py can later join it to the realized N-day drift. Best-effort.
    if dd_results:
        catalyst_log.record_events(context, candidates, dd_results)

    rationale = (f"{len(exits)} rule-exit(s), {len(candidates)} screened candidate(s)"
                 + (" [hostile regime: entries off]" if screen.get("hostile_regime") else "")
                 + (f" [book full: {book_full_note} — entries skipped]" if book_full else "")
                 + (" [entries gated: market closed/stale]" if not context.get("allow_entries") else ""))
    decision_out = {
        "actions": actions,
        "rationale": rationale,
        "screen": screen,
        "dd": dd_results,
        "manage": manage_results,        # Tier-2 hold re-assessments (keep/trim/exit/add)
        "token_usage": usage_summary(),  # exact per-tick headless-Claude token + cost rollup
    }
    TICK.mkdir(parents=True, exist_ok=True)
    (TICK / "decision_latest.json").write_text(json.dumps(decision_out, indent=2))

    n_commit = sum(1 for d in dd_results if d["decision"] == "commit")
    n_error = sum(1 for d in dd_results if d["decision"] == "error")
    n_reject = len(dd_results) - n_commit - n_error

    # --- human-readable decision trail: WHY each name did / didn't trade (counts alone hide it) ---
    for e in exits:
        print(f"  EXIT {e.get('symbol', '?')}: {e.get('reason', '')}")
    if candidates:
        print(f"screen: {len(exits)} exit(s), {len(candidates)} candidate(s): "
              + ", ".join(fmt_candidate(c) for c in candidates))
        if book_full:
            print(f"  BOOK FULL: {book_full_note} — Stage-2 entries skipped this tick")
    else:
        # No candidates -> say why (gate / regime / nothing cleared the bar), not just "0".
        if not context.get("allow_entries"):
            note = f" [entries gated: {context.get('stale_reason') or 'market closed/stale'}]"
        elif screen.get("hostile_regime"):
            note = " [hostile regime: entries off]"
        else:
            note = " [no mover cleared the screen]"
        print(f"screen: {len(exits)} exit(s), 0 candidate(s){note}")
    for d in dd_results:
        print(fmt_dd_line(d))
    not_dd = len(candidates) - len(dd_results)
    if context.get("allow_entries") and not_dd > 0:
        print(f"  (+{not_dd} more candidate(s) not DD'd this tick — MAX_DD_CANDIDATES={max_dd})")
    print(f"DD: {n_commit} commit / {n_reject} reject / {n_error} error -> {len(actions)} action(s)")
    for m in manage_results:
        print(f"  MANAGE {m.get('symbol')}: {(m.get('action') or '?').upper()} — "
              f"{(m.get('reason') or m.get('error') or '')[:90]}")
    if manage_results:
        n_act = sum(1 for m in manage_results if m.get('action') in ('trim', 'exit', 'add'))
        print(f"MANAGE: {len(manage_results)} holding(s) re-checked, {n_act} acted")
    tu = decision_out["token_usage"]
    if tu["n_calls"]:
        print(f"TOKENS: {tu['n_calls']} call(s), {tu['total_tokens']:,} tok "
              f"(in {tu['input_tokens']:,} / out {tu['output_tokens']:,} / "
              f"cache_read {tu['cache_read_input_tokens']:,}) ~${tu['cost_usd']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
