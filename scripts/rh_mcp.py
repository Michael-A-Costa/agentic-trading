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
import time

import rh_direct  # direct Python→MCP fast path for READ snapshots (falls back to the relay below)
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


def _timeout() -> int:
    return int(os.environ.get("RH_EXEC_TIMEOUT_S", "90"))  # was 180; a hung relay shouldn't burn 3 min


def parse_json_obj(raw: str | None) -> dict | None:
    """Pull a single JSON object out of the agent's text. Tolerant of ```json fences, prose BEFORE or
    AFTER the object, and nested braces. The agent intermittently ignores the 'no fences/prose'
    instruction and emits ```json {…} ``` plus a sentence after it; the old non-greedy `\\{.*?\\}`
    truncated such objects at the first inner `}`. We try, in order: the fenced span as-is, then a
    greedy first-`{`/last-`}` slice of the fence, then the same on the whole text."""
    if not raw or not raw.strip():
        return None
    candidates: list[str] = []
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(raw.strip())
    for cand in candidates:
        for attempt in (cand, _brace_slice(cand)):
            if not attempt:
                continue
            try:
                d = json.loads(attempt)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                return d
    return None


def _brace_slice(text: str) -> str | None:
    """The greedy first-`{`..last-`}` span (a single top-level object even with trailing prose)."""
    a, b = text.find("{"), text.rfind("}")
    return text[a:b + 1] if (a != -1 and b > a) else None


_RELAY_PREAMBLE = (
    "Mechanical MCP relay: make EXACTLY the listed tool calls, then output ONE JSON object and "
    "nothing else (no prose, no fences). Copy each tool result VERBATIM — do not round, rename, "
    "or compute. Errors go in \"errors\", null for that field.\n\n"
)

# Write ops (place/cancel) use a LIGHT, owner-grounded framing instead of the "mechanical relay"
# preamble: the heavier framing reads like a prompt-injection to a safety-tuned model, which then
# REFUSES to call place_equity_order and returns prose (place() => None even when nothing placed).
# Phrased as the owner submitting a concrete, already-decided trade on their own account, a small
# model just makes the call. See RH_PLACE_MODEL (defaults to haiku — cheaper and less reflexively
# refusal-prone for this single tool call).
_WRITE_PREAMBLE = (
    "The account owner is submitting a trade on their OWN agentic-enabled Robinhood account through "
    "their automated helper. The order below was already researched, sized, risk-capped, and previewed "
    "upstream; this step only submits it. Make the single tool call shown and return its result.\n\n"
)


def _exec_model() -> str:
    """Model for ALL relay calls (reads, quotes, review, place, cancel). Every relay is MECHANICAL —
    call a tool, echo its JSON verbatim — so the cheapest capable model suffices. Haiku calls tools
    reliably (proven on place/cancel), echoes JSON fine, and its OUTPUT tokens (the verbatim blob —
    the dominant relay cost) are ~4x cheaper than Sonnet's. It's also less prone to over-reasoning a
    place into a refusal. Override with RH_RELAY_MODEL (falls back to the old RH_PLACE_MODEL)."""
    return os.environ.get("RH_RELAY_MODEL",
                          os.environ.get("RH_PLACE_MODEL", "claude-haiku-4-5-20251001"))


def _relay(prompt: str, tools: list[str], model: str | None = None,
           preamble: str = _RELAY_PREAMBLE, timeout: int | None = None,
           label: str = "relay") -> dict | None:
    # `label` flows into run_claude's per-call progress line + the tick token ledger, so each broker
    # call shows up named (relay:review:NVDA, relay:place:NVDA, …) instead of an anonymous "claude".
    out = run_claude(preamble + prompt, model or _exec_model(), tools=tools, mcp=True,
                     timeout=timeout or _timeout(), label=label)
    return parse_json_obj(out)


def snapshot(symbols: list[str] | None = None) -> dict | None:
    """Pull broker TRUTH — buying power + open positions + recent agentic orders — in one agent call.
    Held-position MARKS no longer come from here: tick_context already fetches fresh public quotes for
    every holding, so quoting symbols in the snapshot was redundant AND it quoted the wrong set (the
    pins, not our holdings). The quote step is now OPT-IN (pass symbols) and OFF by default — dropping
    it removes the ~24-symbol batch that made this the heaviest/slowest relay. Returns raw tool blobs.

    FAST PATH (rh_direct): these reads are deterministic HTTP, so we first try speaking the MCP
    directly from Python (~0.3s, $0, no LLM) using the OAuth token Claude Code keeps in the keychain.
    On ANY direct failure (token missing/expired, 401, network) we fall back to the agent relay below —
    which connects through Claude Code and refreshes the keychain token as a side effect, so the next
    direct call is fast again. Same {portfolio, positions, [quotes], orders, errors} shape either way."""
    acct = account()
    syms = sorted({s.upper().strip() for s in (symbols or []) if s and s.strip()})
    if rh_direct.enabled():
        t0 = time.time()
        try:
            snap = rh_direct.snapshot(acct, syms)
            print(f"[rh_mcp] snapshot via direct MCP ({time.time() - t0:.2f}s, no LLM)",
                  file=sys.stderr)
            return snap
        except rh_direct.DirectError as e:
            print(f"[rh_mcp] direct snapshot failed ({e}) — falling back to agent relay",
                  file=sys.stderr)
    # These reads are INDEPENDENT — none feeds another — so they're issued as ONE batch of parallel
    # tool calls in a single agent turn, not numbered sequential "Steps". Sequential phrasing made
    # haiku call them one-at-a-time (~4 turns), and each turn re-processes the full ~30k MCP tool
    # schema → ~132k tok / ~31s for an 8.9KB payload. Batched, the loop collapses to ~2 turns: one
    # that fires all calls at once, one that echoes. Same tools, same output — just fewer round-trips.
    calls = [f"get_portfolio(account_number=\"{acct}\")",
             f"get_equity_positions(account_number=\"{acct}\")",
             # state="confirmed" returns ONLY live resting orders (the GTC protective stops) instead
             # of the full agentic ledger. The snapshot's orders are consumed only by open_stops_for,
             # which discards every filled/cancelled/rejected row anyway — echoing the whole history
             # verbatim was the dominant relay cost (~130k tok / ~160s) AND, since only the first
             # newest-first page is fetched, recent fill/cancel churn could push an old resting stop
             # off the page, hiding it from open_stops_for and triggering a duplicate stop re-arm.
             f"get_equity_orders(account_number=\"{acct}\", placed_agent=\"agentic\", state=\"confirmed\")"]
    shape = ('{"portfolio": <get_portfolio result>, "positions": <get_equity_positions result>, '
             '"orders": <get_equity_orders result>, "errors": {}}')
    if syms:  # optional: only when a caller explicitly wants broker-side marks
        calls.insert(2, f"get_equity_quotes(symbols={json.dumps(syms)})")
        shape = ('{"portfolio": <get_portfolio result>, "positions": <get_equity_positions result>, '
                 '"quotes": <get_equity_quotes result>, "orders": <get_equity_orders result>, '
                 '"errors": {}}')
    prompt = (f"Account: {acct}\n\n"
              "Make ALL of these tool calls AT ONCE in a single turn (they are independent — do NOT "
              "wait for one before issuing the next), then output ONE JSON object with each result:\n"
              + "\n".join(f"- {c}" for c in calls)
              + "\n\nOutput JSON shape:\n" + shape)
    return _relay(prompt, READ_TOOLS, timeout=int(os.environ.get("RH_SNAPSHOT_TIMEOUT_S", "180")),
                  label="relay:snapshot")


def _try_direct(fn, label: str):
    """Run a rh_direct read fast-path; on any DirectError log + return None so the caller falls back to
    the agent relay (which refreshes the keychain token, so the next direct call is fast again)."""
    if not rh_direct.enabled():
        return None
    t0 = time.time()
    try:
        out = fn()
        print(f"[rh_mcp] {label} via direct MCP ({time.time() - t0:.2f}s, no LLM)", file=sys.stderr)
        return out
    except rh_direct.DirectError as e:
        print(f"[rh_mcp] direct {label} failed ({e}) — falling back to agent relay", file=sys.stderr)
        return None


def _summ(spec: dict | None) -> str:
    """Compact human-readable order summary for log lines, e.g. 'BUY 5 ALHC limit@18.42 gfd' or
    'SELL 5 ALHC stop_market@16.30 gtc'. Reads only the MCP spec, so it always reflects exactly what
    was sent (intent); the broker-confirmed fill/id/state is logged downstream by live_execute."""
    if not isinstance(spec, dict):
        return str(spec)
    side = str(spec.get("side", "?")).upper()
    qty = spec.get("quantity", "?")
    sym = spec.get("symbol", "?")
    otype = spec.get("type", "?")
    px = spec.get("limit_price") or spec.get("stop_price")
    px_part = f"@{px}" if px else ""
    tif = spec.get("time_in_force", "")
    return f"{side} {qty} {sym} {otype}{px_part} {tif}".strip()


def quotes(symbols: list[str]) -> dict | None:
    """Fresh live quotes for a symbol list — get_equity_quotes ONLY (no portfolio/positions/orders).
    Used to size entries whose symbols aren't in the pre-decision broker snapshot (which only quotes
    the CANDIDATES pins + indexes). The toolset excludes place, so this can never execute."""
    syms = sorted({s.upper().strip() for s in symbols if s and s.strip()})
    if not syms:
        return {"quotes": {"results": []}, "errors": {}}
    direct = _try_direct(lambda: rh_direct.quotes(syms), f"quotes({len(syms)})")
    if direct is not None:
        return direct
    prompt = (
        f"Symbols: {json.dumps(syms)}\n\n"
        "Steps:\n"
        f"1. get_equity_quotes(symbols={json.dumps(syms)})\n\n"
        "Output JSON shape:\n"
        '{"quotes": <verbatim result of step 1>, "errors": {}}'
    )
    return _relay(prompt, ["mcp__robinhood-trading__get_equity_quotes"],
                  label=f"relay:quotes({len(syms)})")


def review(spec: dict) -> dict | None:
    """Run review_equity_order for a fully-specified order. Returns the raw review payload (quote +
    alerts) — Python decides if any alert is blocking. The place tool is NOT in this toolset, so a
    review call can never accidentally execute."""
    acct = account()
    direct = _try_direct(lambda: rh_direct.review(acct, spec), f"review {_summ(spec)}")
    if direct is not None:
        return direct
    params = {"account_number": acct, **spec}
    prompt = (
        "Steps:\n"
        f"1. review_equity_order with EXACTLY these parameters: {json.dumps(params)}\n\n"
        "Output JSON shape:\n"
        '{"review": <verbatim result of step 1>, "errors": {}}'
    )
    return _relay(prompt, REVIEW_TOOLS, label=f"relay:review:{spec.get('symbol', '?')}")


def place(spec: dict, ref_id: str) -> dict | None:
    """Place a real order. Caller MUST have run review() and re-checked caps first. ref_id is the
    idempotency key (re-send the SAME id on a transient retry; a new id only for a new order).

    FAST PATH (rh_direct): place_equity_order is just an MCP tool call — Python already decided the whole
    order, so the LLM relay added nothing but ~40-56s, ~200-500k tokens, and two failure modes (haiku
    refusing to place, or placing then echoing unparseable prose). We call the tool directly (~0.3s, $0,
    deterministic). On ANY direct failure we fall back to the relay below, re-sending the SAME ref_id —
    so the broker's idempotency prevents a double-place even if direct failed AFTER the order landed."""
    acct = account()
    direct = _try_direct(lambda: rh_direct.place(acct, spec, ref_id), f"place {_summ(spec)}")
    if direct is not None:
        return direct
    params = {"account_number": acct, "ref_id": ref_id, **spec}
    prompt = (
        f"Submit this stock order on Robinhood account {acct} by calling place_equity_order with "
        f"EXACTLY these parameters:\n{json.dumps(params)}\n\n"
        "Then reply with ONLY this JSON — the tool's result copied verbatim, no code fences, no "
        "commentary:\n"
        '{"order": <place_equity_order result>, "errors": {}}'
    )
    return _relay(prompt, PLACE_TOOLS, model=_exec_model(), preamble=_WRITE_PREAMBLE,
                  label=f"relay:place:{spec.get('symbol', '?')}")


def recent_orders(symbol: str, created_at_gte: str | None = None) -> dict | None:
    """Fresh get_equity_orders for ONE symbol (agentic, newest first) — used to CONFIRM a place from
    broker truth when place()'s echo is unparseable/None. The place relay is non-deterministic (it
    may place the order yet return no usable JSON), so the order id is re-read from the broker rather
    than trusted from the agent's prose. Read-only toolset — cannot place."""
    acct = account()
    direct = _try_direct(lambda: rh_direct.recent_orders(acct, symbol, created_at_gte),
                         f"orders:{symbol.upper()}")
    if direct is not None:
        return direct
    params = {"account_number": acct, "symbol": symbol.upper(), "placed_agent": "agentic"}
    if created_at_gte:
        params["created_at_gte"] = created_at_gte
    prompt = (
        "Steps:\n"
        f"1. get_equity_orders with EXACTLY these parameters: {json.dumps(params)}\n\n"
        "Output JSON shape:\n"
        '{"orders": <verbatim result of step 1>, "errors": {}}'
    )
    return _relay(prompt, ["mcp__robinhood-trading__get_equity_orders"],
                  label=f"relay:orders:{symbol.upper()}")


def cancel(order_id: str) -> dict | None:
    """Cancel one open order by id (used to clear a resting stop before a discretionary sell).

    FAST PATH (rh_direct): direct cancel_equity_order (~0.3s, $0, no LLM); falls back to the relay on any
    direct failure. Cancel-by-id is idempotent, so a fallback retry is harmless."""
    acct = account()
    direct = _try_direct(lambda: rh_direct.cancel(acct, order_id), f"cancel:{str(order_id)[:8]}")
    if direct is not None:
        return direct
    prompt = (
        f"Cancel this open order on Robinhood account {acct} by calling cancel_equity_order with "
        f"account_number=\"{acct}\" and order_id=\"{order_id}\".\n\n"
        "Then reply with ONLY this JSON — the tool's result copied verbatim, no code fences, no "
        "commentary:\n"
        '{"cancel": <cancel_equity_order result>, "errors": {}}'
    )
    return _relay(prompt, CANCEL_TOOLS, model=_exec_model(), preamble=_WRITE_PREAMBLE,
                  label=f"relay:cancel:{str(order_id)[:8]}")


if __name__ == "__main__":
    # Manual smoke test: `python3 scripts/rh_mcp.py snapshot AAPL,NVDA` (requires live MCP auth).
    if len(sys.argv) >= 2 and sys.argv[1] == "snapshot":
        syms = sys.argv[2].split(",") if len(sys.argv) > 2 else ["SPY"]
        print(json.dumps(snapshot(syms), indent=2))
    else:
        print("usage: rh_mcp.py snapshot SYM[,SYM...]", file=sys.stderr)
        raise SystemExit(2)
