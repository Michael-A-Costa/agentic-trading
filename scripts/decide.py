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
from pathlib import Path

from apply_decision import extract_decision  # sibling: robust JSON extraction

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
TICK = REPO / "data" / "tick"
PYEXE = sys.executable or "python3"


def claude_bin() -> str:
    return (os.environ.get("AGENTIC_CLAUDE") or shutil.which("claude")
            or str(Path.home() / ".local/bin/claude"))


def run_claude(prompt: str, model: str, websearch: bool = False, timeout: int = 240) -> str:
    cmd = [claude_bin(), "-p", prompt, "--model", model, "--output-format", "text"]
    if websearch:
        cmd += ["--allowedTools", "WebSearch", "--dangerously-skip-permissions"]
    else:
        cmd += ["--strict-mcp-config"]  # no MCP/tools needed for the cheap screen
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout or ""
    except subprocess.TimeoutExpired:
        return ""


def main() -> int:
    context = json.loads((TICK / "context_latest.json").read_text())
    packet = json.loads((TICK / "packet_latest.json").read_text())
    caps = context["caps"]
    screen_model = os.environ.get("SCREEN_MODEL", "claude-haiku-4-5-20251001")
    dd_model = os.environ.get("DD_MODEL", "claude-opus-4-8")
    max_dd = int(os.environ.get("MAX_DD_CANDIDATES", "2"))

    # --- Stage 1: screen ---
    screen_raw = run_claude((SCRIPTS / "tick_prompt.txt").read_text() + json.dumps(packet),
                            screen_model, timeout=90)
    screen = extract_decision(screen_raw)  # tolerant; gives {} fields if malformed
    exits = screen.get("exits") or []
    candidates = screen.get("entry_candidates") or []

    actions = [{"symbol": e.get("symbol"), "side": "sell", "reason": e.get("reason", "")}
               for e in exits if e.get("symbol")]

    # --- Stage 2: deep DD + Opus commit (only on real candidates, only when entries allowed) ---
    dd_results = []
    if context.get("allow_entries") and candidates:
        cash = context["portfolio"]["cash"]
        for c in candidates[:max_dd]:
            sym = str(c.get("symbol", "")).upper().strip()
            if not sym:
                continue
            subprocess.run([PYEXE, str(SCRIPTS / "dd_probe.py"), sym],
                           capture_output=True, text=True, timeout=60)
            dd_file = TICK / f"dd_{sym}.json"
            dd = json.loads(dd_file.read_text()) if dd_file.exists() else {"symbol": sym, "error": "no_dd"}
            dd_input = json.dumps({
                "symbol": sym,
                "screen_reason": c.get("reason", ""),
                "regime": packet.get("regime"),
                "sizing": {"MAX_POSITION_USD": caps["MAX_POSITION_USD"], "available_cash": round(cash, 2)},
                "dd": dd,
            })
            commit = extract_decision(run_claude((SCRIPTS / "dd_prompt.txt").read_text() + dd_input,
                                                 dd_model, websearch=True, timeout=300))
            decision = (commit.get("decision") or "reject").lower()
            dd_results.append({"symbol": sym, "decision": decision,
                               "conviction": commit.get("conviction"),
                               "dollar_amount": commit.get("dollar_amount"),
                               "reason": commit.get("reason", ""),
                               "catalysts": commit.get("catalysts", []),
                               "risks": commit.get("risks", [])})
            if decision == "commit" and commit.get("dollar_amount"):
                actions.append({"symbol": sym, "side": "buy",
                                "dollar_amount": commit["dollar_amount"],
                                "reason": f"[DD/{commit.get('conviction', '?')}] {commit.get('reason', '')}"})

    decision_out = {
        "actions": actions,
        "rationale": screen.get("rationale", ""),
        "screen": {"exits": exits, "entry_candidates": candidates},
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
