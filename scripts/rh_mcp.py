#!/usr/bin/env python3
"""
rh_mcp.py — the ONLY bridge from Python to the Robinhood trading MCP.

Python can't speak MCP; only a Claude agent can. Each function here spawns ONE headless `claude`
with the MINIMUM Robinhood tools for its job and a strict instruction to emit a single JSON object,
then parses that JSON. The agent is a mechanical RELAY: every sizing / cap / gating decision lives
in Python (live_execute.py), never in the agent. Truth is always RE-READ from the broker — the
agent's prose is never trusted for a fill; we reconcile against get_equity_orders / positions.

Design choices that keep the safety in code:
  - review and place are SEPARATE agent calls. The agent never decides whether an alert is
    blocking — it returns the raw review, Python decides, and only then calls place(). So a
    misbehaving agent can't place against a blocking alert: the place tool isn't even in the
    review agent's toolset.
  - the account is hard-pinned to AGENTIC_ACCOUNT; every helper refuses a missing/other account.
  - place() carries a caller-supplied ref_id (UUID) for idempotency across retries/crashes.
"""
from __future__ import annotations

import json
import os
import re
import sys

from decide import run_claude  # reuse the headless-claude runner (handles --mcp-config/--allowedTools)

# Minimum toolsets per role — least privilege. No web, no filesystem; the relay only touches RH.
READ_TOOLS = [
    "mcp__robinhood-trading__get_portfolio",
    "mcp__robinhood-trading__get_equity_positions",
    "mcp__robinhood-trading__get_equity_quotes",
    "mcp__robinhood-trading__get_equity_orders",
]
REVIEW_TOOLS = ["mcp__robinhood-trading__get_equity_quotes",
                "mcp__robinhood-trading__review_equity_order"]
PLACE_TOOLS = ["mcp__robinhood-trading__place_equity_order"]
CANCEL_TOOLS = ["mcp__robinhood-trading__cancel_equity_order"]


def account() -> str:
    acct = (os.environ.get("AGENTIC_ACCOUNT") or "").strip()
    if not acct:
        raise RuntimeError("AGENTIC_ACCOUNT is not set — refusing to touch the broker without a pinned account")
    return acct


def _model() -> str:
    # Execution is mechanical (call tools, echo JSON); a capable model is used for reliable
    # tool-calling + strict JSON. Override with RH_EXEC_MODEL.
    return os.environ.get("RH_EXEC_MODEL", os.environ.get("DD_MODEL", "claude-sonnet-4-6"))


def _timeout() -> int:
    return int(os.environ.get("RH_EXEC_TIMEOUT_S", "180"))


def parse_json_obj(raw: str | None) -> dict | None:
    """Pull a single JSON object out of the agent's text (tolerates ```json fences / prose)."""
    if not raw or not raw.strip():
        return None
    text: str = raw.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        a, b = text.find("{"), text.rfind("}")
        if a != -1 and b != -1 and b > a:
            text = text[a:b + 1]
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        return None


_RELAY_PREAMBLE = (
    "You are a MECHANICAL relay to the Robinhood trading MCP. Make EXACTLY the tool calls listed "
    "below — no more, no fewer — then output ONE JSON object and NOTHING else (no prose, no fences). "
    "Copy each tool's result VERBATIM into the JSON: do not round, rename, summarize, infer, or "
    "compute anything. If a tool errors, put its error string in \"errors\" and use null for that "
    "field. Never place, modify, or cancel any order beyond the explicit steps.\n\n"
)


def _relay(prompt: str, tools: list[str]) -> dict | None:
    out = run_claude(_RELAY_PREAMBLE + prompt, _model(), tools=tools, mcp=True, timeout=_timeout())
    return parse_json_obj(out)


def snapshot(symbols: list[str]) -> dict | None:
    """Pull buying power + open positions + held/candidate quotes + recent agentic orders in one
    agent call. Returns raw tool blobs (live_execute / tick_context parse defensively)."""
    acct = account()
    syms = sorted({s.upper().strip() for s in symbols if s and s.strip()})
    prompt = (
        f"Account: {acct}\nSymbols: {json.dumps(syms)}\n\n"
        "Steps:\n"
        f"1. get_portfolio(account_number=\"{acct}\")\n"
        f"2. get_equity_positions(account_number=\"{acct}\")\n"
        f"3. get_equity_quotes(symbols={json.dumps(syms)})\n"
        f"4. get_equity_orders(account_number=\"{acct}\", placed_agent=\"agentic\")\n\n"
        "Output JSON shape:\n"
        '{"portfolio": <result of step 1>, "positions": <result of step 2>, '
        '"quotes": <result of step 3>, "orders": <result of step 4>, "errors": {}}'
    )
    return _relay(prompt, READ_TOOLS)


def review(spec: dict) -> dict | None:
    """Run review_equity_order for a fully-specified order. Returns the raw review payload (quote +
    alerts) — Python decides if any alert is blocking. The place tool is NOT in this toolset, so a
    review call can never accidentally execute."""
    acct = account()
    params = {"account_number": acct, **spec}
    prompt = (
        "Steps:\n"
        f"1. review_equity_order with EXACTLY these parameters: {json.dumps(params)}\n\n"
        "Output JSON shape:\n"
        '{"review": <verbatim result of step 1>, "errors": {}}'
    )
    return _relay(prompt, REVIEW_TOOLS)


def place(spec: dict, ref_id: str) -> dict | None:
    """Place a real order. Caller MUST have run review() and re-checked caps first. ref_id is the
    idempotency key (re-send the SAME id on a transient retry; a new id only for a new order)."""
    acct = account()
    params = {"account_number": acct, "ref_id": ref_id, **spec}
    prompt = (
        "Steps:\n"
        f"1. place_equity_order with EXACTLY these parameters: {json.dumps(params)}\n\n"
        "Output JSON shape:\n"
        '{"order": <verbatim result of step 1>, "errors": {}}'
    )
    return _relay(prompt, PLACE_TOOLS)


def cancel(order_id: str) -> dict | None:
    """Cancel one open order by id (used to clear a resting stop before a discretionary sell)."""
    acct = account()
    prompt = (
        "Steps:\n"
        f"1. cancel_equity_order(account_number=\"{acct}\", order_id=\"{order_id}\")\n\n"
        "Output JSON shape:\n"
        '{"cancel": <verbatim result of step 1>, "errors": {}}'
    )
    return _relay(prompt, CANCEL_TOOLS)


if __name__ == "__main__":
    # Manual smoke test: `python3 scripts/rh_mcp.py snapshot AAPL,NVDA` (requires live MCP auth).
    if len(sys.argv) >= 2 and sys.argv[1] == "snapshot":
        syms = sys.argv[2].split(",") if len(sys.argv) > 2 else ["SPY"]
        print(json.dumps(snapshot(syms), indent=2))
    else:
        print("usage: rh_mcp.py snapshot SYM[,SYM...]", file=sys.stderr)
        raise SystemExit(2)
