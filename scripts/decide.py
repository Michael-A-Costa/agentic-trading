#!/usr/bin/env python3
"""
decide.py — two-stage decision orchestrator for one tick. Produces the final action list; the
deterministic executor (apply_decision.py) still re-checks caps and logs.

  Stage 1  (cheap, every TRADE tick): SCREEN_MODEL reads the compact packet -> {exits, entry_candidates}
  Stage 2  (only if there are entry_candidates AND allow_entries): for each shortlisted name,
           dd_probe.py gathers deep quant DD, then DD_MODEL (Opus) + web news/catalyst search
           decides commit/reject + size. Default is REJECT.

Exits execute immediately (mechanical risk mgmt). Entries only after Stage-2 commits. Output:
data/tick/decision_latest.json = {actions, rationale, screen, dd}. The LLMs only judge; all data
gathering is scripts.
"""
from __future__ import annotations

import json
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
               timeout: int = 360) -> str:
    """Headless Claude. --strict-mcp-config => only the servers we pass (none, or the RH MCP)."""
    cmd = [claude_bin(), "-p", prompt, "--model", model, "--output-format", "text",
           "--strict-mcp-config"]
    if mcp:
        cmd += ["--mcp-config", str(REPO / ".mcp.json")]
    if tools:
        cmd += ["--allowedTools", *tools, "--dangerously-skip-permissions"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout or ""
    except subprocess.TimeoutExpired:
        return ""


# Stage-2 DD agent toolset: quant is script-gathered; the agent adds live quote + news research.
DD_TOOLS = ["WebSearch", "WebFetch", "mcp__robinhood-trading__get_equity_quotes"]


def run_dd(sym: str, screen_reason: str, regime: dict, caps: dict, cash: float, dd_model: str) -> dict:
    """Stage-2 research agent for one symbol: deep quant probe + multi-tool news/catalyst commit."""
    subprocess.run([PYEXE, str(SCRIPTS / "dd_probe.py"), sym],
                   capture_output=True, text=True, timeout=60)
    dd_file = TICK / f"dd_{sym}.json"
    dd = json.loads(dd_file.read_text()) if dd_file.exists() else {"symbol": sym, "error": "no_dd"}
    dd_input = json.dumps({
        "symbol": sym,
        "screen_reason": screen_reason,
        "regime": regime,
        "sizing": {"MAX_POSITION_USD": caps["MAX_POSITION_USD"], "available_cash": round(cash, 2)},
        "dd": dd,
    })
    out = run_claude((SCRIPTS / "dd_prompt.txt").read_text() + dd_input,
                     dd_model, tools=DD_TOOLS, mcp=True, timeout=420)
    commit = extract_decision(out)
    return {"symbol": sym, "decision": (commit.get("decision") or "reject").lower(),
            "conviction": commit.get("conviction"), "dollar_amount": commit.get("dollar_amount"),
            "reason": commit.get("reason", ""), "catalysts": commit.get("catalysts", []),
            "risks": commit.get("risks", [])}


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
        cash = context["portfolio"]["cash"]
        for c in candidates[:max_dd]:
            sym = str(c.get("symbol", "")).upper().strip()
            if not sym:
                continue
            cached = cache.get(sym)
            if cached and (now - cached.get("ts", 0)) < ttl:
                res = {**cached["result"], "cached": True,
                       "cached_age_min": int((now - cached["ts"]) / 60)}
            else:
                res = run_dd(sym, c.get("reason", ""), regime, caps, cash, dd_model)
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

    print(f"screen: {len(exits)} exit(s), {len(candidates)} candidate(s); "
          f"DD: {sum(1 for d in dd_results if d['decision'] == 'commit')} commit / "
          f"{sum(1 for d in dd_results if d['decision'] != 'commit')} reject; "
          f"final actions: {len(actions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
