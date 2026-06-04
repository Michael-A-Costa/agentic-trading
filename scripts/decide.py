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
import time
from pathlib import Path

import stock_memory as memory                # sibling: long-term per-symbol eval memory + exclusions
from apply_decision import extract_decision  # sibling: robust JSON extraction

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
TICK = REPO / "data" / "tick"
DD_CACHE = REPO / "data" / "dd_cache.json"
PYEXE = sys.executable or "python3"


def load_cache() -> dict:
    try:
        return json.loads(DD_CACHE.read_text())
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict) -> None:
    DD_CACHE.parent.mkdir(parents=True, exist_ok=True)
    DD_CACHE.write_text(json.dumps(cache, indent=2))


def claude_bin() -> str:
    return (os.environ.get("AGENTIC_CLAUDE") or shutil.which("claude")
            or str(Path.home() / ".local/bin/claude"))


def run_claude(prompt: str, model: str, tools: list | None = None, mcp: bool = False,
               timeout: int = 360) -> str | None:
    """Headless Claude. --strict-mcp-config => only the servers we pass (none, or the RH MCP).

    Returns stdout on success, or None on timeout / non-zero exit / empty output — so the caller
    can tell a model FAILURE (retry next tick, don't cache) apart from a real 'reject' verdict.
    """
    cmd = [claude_bin(), "-p", prompt, "--model", model, "--output-format", "text",
           "--strict-mcp-config"]
    if mcp:
        cmd += ["--mcp-config", str(REPO / ".mcp.json")]
    if tools:
        cmd += ["--allowedTools", *tools, "--dangerously-skip-permissions"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        sys.stderr.write(f"[decide] claude invocation failed: {e}\n")
        return None
    if r.returncode != 0:
        sys.stderr.write(f"[decide] claude exit {r.returncode}: {(r.stderr or '')[:300]}\n")
        return None
    return r.stdout if (r.stdout and r.stdout.strip()) else None


# Stage-2 DD agent toolset: quant is script-gathered; the agent adds live quote + news research.
DD_TOOLS = ["WebSearch", "WebFetch", "mcp__robinhood-trading__get_equity_quotes"]


def fmt_candidate(c: dict) -> str:
    """One screened entry candidate -> compact signal string for the log."""
    return f"{c.get('symbol', '?')}(+{c.get('intraday_pct')}% rel {c.get('rel_strength')})"


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
        "screen_signal": {"intraday_pct": c.get("intraday_pct"),
                          "rel_strength_vs_spy": c.get("rel_strength"),
                          "range_pos": c.get("range_pos")},  # near 1.0 = at the day's high (extended)
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
                     dd_model, tools=DD_TOOLS, mcp=True, timeout=dd_timeout)
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
            "catalysts": commit.get("catalysts", []), "risks": commit.get("risks", []),
            "next_earnings_date": commit.get("next_earnings_date"),
            "never_buy": bool(commit.get("never_buy")),          # structural disqualifier -> exclude
            "never_buy_reason": commit.get("never_buy_reason")}


def main() -> int:
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

    # --- Stage 2: deep DD + commit, with a per-symbol TTL cache (split commit/reject TTLs) ---
    # Commits are cached for DD_CACHE_TTL_MIN (longer): expensive to recompute, and execution still
    # re-checks fresh price + caps + allow_entries in apply_decision, so reusing one briefly never
    # trades on a stale price. Rejects are cached for a SHORTER DD_REJECT_TTL_MIN so a name whose
    # setup is improving re-evaluates within the hour — but NOT every tick, which (with dynamic
    # discovery surfacing the same top movers repeatedly) would re-burn a ~85s Sonnet+web call on the
    # same reject every 5 minutes. Errors are never cached (retry next tick). Set DD_REJECT_TTL_MIN=0
    # to disable reject caching (re-DD every tick).
    dd_results = []
    cache = load_cache()
    commit_ttl = int(os.environ.get("DD_CACHE_TTL_MIN", "30")) * 60
    reject_ttl = int(os.environ.get("DD_REJECT_TTL_MIN", "20")) * 60
    now = time.time()
    cache_dirty = False
    if context.get("allow_entries") and candidates:
        positions_ctx = context.get("positions", [])
        portfolio = {
            "cash": context["portfolio"]["cash"],
            "exposure": context["portfolio"].get("positions_value", 0.0),
            "open_positions": context["portfolio"].get("open_positions", len(positions_ctx)),
            "held": [p["symbol"] for p in positions_ctx],
        }
        shortlist = []
        for c in candidates[:max_dd]:
            sym = str(c.get("symbol", "")).upper().strip()
            if sym:
                shortlist.append((sym, c))

        # Serve fresh cached verdicts synchronously (commits live longer than rejects; errors are
        # never cached so they retry; reject_ttl=0 disables reject reuse). Cache misses need a fresh
        # Stage-2 call and go to the parallel pool below.
        cache_hits = {}
        fresh_jobs = []
        for sym, c in shortlist:
            res = None
            cached = cache.get(sym)
            if cached:
                cdec = (cached.get("result") or {}).get("decision")
                age = now - cached.get("ts", 0)
                ttl = commit_ttl if cdec == "commit" else (reject_ttl if cdec == "reject" else 0)
                if ttl > 0 and age < ttl:
                    res = {**cached["result"], "cached": True, "cached_age_min": int(age / 60)}
            if res is not None:
                cache_hits[sym] = res
            else:
                fresh_jobs.append((sym, c))

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
            # Cache commits and rejects (each reused under its own TTL at read time); never cache an
            # error — a transient model/timeout failure must be retried, not frozen.
            if not res.get("cached") and res.get("decision") in ("commit", "reject"):
                cache[sym] = {"ts": now, "result": {k: v for k, v in res.items() if k != "dd_elapsed_s"}}
                cache_dirty = True
                # Persist the main points to long-term memory and auto-exclude a never-buy name.
                memory.record(sym, decision=res["decision"], conviction=res.get("conviction"),
                              reason=res.get("reason", ""), catalysts=res.get("catalysts"),
                              risks=res.get("risks"), next_earnings_date=res.get("next_earnings_date"),
                              never_buy=res.get("never_buy"), never_buy_reason=res.get("never_buy_reason"))
            dd_results.append(res)
            if res.get("decision") == "commit" and res.get("dollar_amount"):
                tag = f"DD/{res.get('conviction', '?')}" + ("/cached" if res.get("cached") else "")
                actions.append({"symbol": sym, "side": "buy",
                                "dollar_amount": res["dollar_amount"],
                                "reason": f"[{tag}] {res.get('reason', '')}"})
        if cache_dirty:
            save_cache(cache)

    rationale = (f"{len(exits)} rule-exit(s), {len(candidates)} screened candidate(s)"
                 + (" [hostile regime: entries off]" if screen.get("hostile_regime") else "")
                 + (" [entries gated: market closed/stale]" if not context.get("allow_entries") else ""))
    decision_out = {
        "actions": actions,
        "rationale": rationale,
        "screen": screen,
        "dd": dd_results,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
