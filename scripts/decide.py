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

import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

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
        "dd": dd,
    })
    out = run_claude((SCRIPTS / "dd_prompt.txt").read_text() + dd_input,
                     dd_model, tools=DD_TOOLS, mcp=True, timeout=420)
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
            "catalysts": commit.get("catalysts", []), "risks": commit.get("risks", [])}


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

    actions = [{"symbol": e.get("symbol"), "side": "sell", "reason": e.get("reason", "")}
               for e in exits if e.get("symbol")]

    # --- Stage 2: deep DD + commit, with a per-symbol TTL cache ---
    # If the same name keeps clearing the screen tick-after-tick, we don't re-burn a ~60s
    # Sonnet+web call each time — we reuse the recent verdict until DD_CACHE_TTL_MIN expires.
    # Execution still re-checks fresh price + caps + allow_entries in apply_decision, so a cached
    # commit never trades on a stale price. Tune DD_CACHE_TTL_MIN (60..1440) to taste.
    dd_results = []
    cache = load_cache()
    ttl = int(os.environ.get("DD_CACHE_TTL_MIN", "180")) * 60
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
        for c in candidates[:max_dd]:
            sym = str(c.get("symbol", "")).upper().strip()
            if not sym:
                continue
            cached = cache.get(sym)
            if cached and (now - cached.get("ts", 0)) < ttl:
                res = {**cached["result"], "cached": True,
                       "cached_age_min": int((now - cached["ts"]) / 60)}
            else:
                res = run_dd(c, regime, caps, portfolio, dd_model)
                # Never cache a failure — a transient timeout / parse error must be retried next
                # tick, not frozen as a verdict that suppresses a good name for the whole TTL.
                if res.get("decision") != "error":
                    cache[sym] = {"ts": now, "result": res}
                    cache_dirty = True
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
    print(f"screen: {len(exits)} exit(s), {len(candidates)} candidate(s); "
          f"DD: {n_commit} commit / {n_reject} reject / {n_error} error; "
          f"final actions: {len(actions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
