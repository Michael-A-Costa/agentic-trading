#!/usr/bin/env python3
"""rh_direct.py — speak the Robinhood MCP (Streamable HTTP / JSON-RPC) DIRECTLY from Python, with NO
headless `claude` agent in the loop.

Why this exists: rh_mcp.snapshot() spins up a whole `claude` (haiku) process and lets it loop to make
3 read-only tool calls — measured ~23-31s, ~95-130k tokens, ~$0.05 per snapshot, and non-deterministic
(it occasionally over-reads to 300k+ tok). Those reads are deterministic HTTP; an LLM adds nothing. This
module makes the SAME calls directly — ~0.3s, $0, deterministic — by reusing the OAuth access token that
Claude Code already maintains for the MCP (macOS keychain item "Claude Code-credentials" →
mcpOAuth.<server>.accessToken).

READS ONLY. Writes (place/cancel) deliberately stay on the agent relay (rh_mcp): their owner-framed
prompt, idempotency ref_id, and verbatim-echo reconciliation are the safety design and aren't worth
re-implementing here.

Auth is best-effort, never authoritative: if the keychain token is missing/expired or the MCP returns
401, we RAISE so the caller (rh_mcp.snapshot) falls back to the agent relay. That relay connects through
Claude Code, which REFRESHES the keychain token as a side effect — so the next direct call is fast again.
We never WRITE the keychain (corrupting Claude Code's credential blob would be far worse than one slow
tick).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

ENDPOINT = os.environ.get("RH_MCP_URL", "https://agent.robinhood.com/mcp/trading")
SERVER_NAME = "robinhood-trading"
KEYCHAIN_SERVICE = "Claude Code-credentials"
PROTOCOL_VERSION = "2025-06-18"
# Refuse a token within this many seconds of expiry — fall back to the relay (which refreshes it)
# rather than racing an expiry mid-tick.
EXPIRY_BUFFER_S = 90


class DirectError(RuntimeError):
    """Any failure of the direct path — caller should fall back to the agent relay."""


def enabled() -> bool:
    return os.environ.get("RH_DIRECT_MCP", "1").strip().lower() not in ("0", "false", "no", "")


def _keychain_token() -> str:
    """Pull the live RH-MCP access token from Claude Code's keychain credential blob. Raises
    DirectError if the item/token is absent or within EXPIRY_BUFFER_S of expiry."""
    try:
        raw = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as e:
        raise DirectError(f"keychain read failed: {e}") from e
    if raw.returncode != 0 or not raw.stdout.strip():
        raise DirectError(f"keychain item {KEYCHAIN_SERVICE!r} not found")
    try:
        creds = json.loads(raw.stdout)
    except ValueError as e:
        raise DirectError(f"keychain blob not JSON: {e}") from e
    for entry in (creds.get("mcpOAuth") or {}).values():
        if not isinstance(entry, dict):
            continue
        if entry.get("serverName") == SERVER_NAME or entry.get("serverUrl") == ENDPOINT:
            tok = entry.get("accessToken")
            if not tok:
                raise DirectError("RH mcpOAuth entry has no accessToken")
            exp_ms = entry.get("expiresAt")
            if isinstance(exp_ms, (int, float)) and (exp_ms / 1000.0 - time.time()) < EXPIRY_BUFFER_S:
                raise DirectError("RH access token expired / about to expire — relay will refresh it")
            return tok
    raise DirectError(f"no mcpOAuth entry for {SERVER_NAME!r} in keychain")


def _parse_body(body: str, ctype: str) -> dict:
    """Streamable-HTTP responses arrive as either application/json or a text/event-stream frame."""
    if "text/event-stream" in ctype:
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise DirectError("empty SSE response")
    return json.loads(body)


def _post(token: str, payload: dict, session: str | None, timeout: float) -> tuple[dict | None, str | None]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(), headers=headers,
                                 method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise DirectError(f"auth rejected ({e.code}) — relay will refresh") from e
        raise DirectError(f"HTTP {e.code}: {e.read()[:200]!r}") from e
    except (urllib.error.URLError, OSError) as e:
        raise DirectError(f"network error: {e}") from e
    sid = resp.headers.get("Mcp-Session-Id")
    body = resp.read().decode()
    if payload.get("method", "").startswith("notifications/") or not body.strip():
        return None, sid
    return _parse_body(body, resp.headers.get("Content-Type", "")), sid


class _Client:
    """One MCP session for the life of a single process (initialize once, reuse the session id)."""

    def __init__(self, token: str, timeout: float):
        self.token = token
        self.timeout = timeout
        init, sid = _post(token, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "rh-direct", "version": "1.0"}},
        }, None, timeout)
        if not isinstance(init, dict) or "result" not in init:
            raise DirectError(f"initialize failed: {str(init)[:200]}")
        self.session = sid
        _post(token, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
              sid, timeout)

    def call(self, name: str, arguments: dict) -> dict:
        """Invoke one MCP tool, return its result payload as a parsed dict (the {data, guide} blob the
        agent relay echoes verbatim). Raises DirectError on a JSON-RPC error or a tool isError."""
        out, _ = _post(self.token, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }, self.session, self.timeout)
        if not isinstance(out, dict):
            raise DirectError(f"{name}: no response")
        if out.get("error"):
            raise DirectError(f"{name}: rpc error {out['error']}")
        res = out.get("result") or {}
        content = res.get("content") or []
        text = content[0].get("text") if content and isinstance(content[0], dict) else None
        if text is None:
            raise DirectError(f"{name}: no text content")
        try:
            payload = json.loads(text)
        except ValueError:
            payload = {"data": text}
        if res.get("isError"):
            raise DirectError(f"{name}: tool error {str(payload)[:200]}")
        return payload


def _run(calls: list[tuple[str, str, dict]]) -> dict:
    """Run an ordered list of (result_key, tool_name, arguments) over ONE MCP session and return
    {result_key: <tool payload>, ..., "errors": {}} — the same envelope the agent relay echoes. Raises
    DirectError on any failure so the caller falls back to the relay."""
    if not enabled():
        raise DirectError("RH_DIRECT_MCP disabled")
    timeout = float(os.environ.get("RH_DIRECT_TIMEOUT_S", "20"))
    client = _Client(_keychain_token(), timeout)
    out: dict = {}
    for key, tool, args in calls:
        out[key] = client.call(tool, args)
    out["errors"] = {}
    return out


def snapshot(account: str, symbols: list[str] | None = None) -> dict:
    """Direct equivalent of rh_mcp.snapshot — buying power + positions + resting orders (+ optional
    quotes), in the SAME {portfolio, positions, [quotes], orders, errors} shape parse_snapshot consumes."""
    syms = sorted({s.upper().strip() for s in (symbols or []) if s and s.strip()})
    calls: list[tuple[str, str, dict]] = [
        ("portfolio", "get_portfolio", {"account_number": account}),
        ("positions", "get_equity_positions", {"account_number": account})]
    if syms:  # optional broker-side marks — keep ordering so the JSON shape mirrors the relay's
        calls.append(("quotes", "get_equity_quotes", {"symbols": syms}))
    calls.append(("orders", "get_equity_orders",
                  {"account_number": account, "placed_agent": "agentic", "state": "confirmed"}))
    return _run(calls)


def quotes(symbols: list[str]) -> dict:
    """Direct equivalent of rh_mcp.quotes — fresh live quotes only, {"quotes": <result>, "errors": {}}."""
    syms = sorted({s.upper().strip() for s in symbols if s and s.strip()})
    if not syms:  # mirror rh_mcp.quotes' empty short-circuit (never hits the wire)
        return {"quotes": {"results": []}, "errors": {}}
    return _run([("quotes", "get_equity_quotes", {"symbols": syms})])


def recent_orders(account: str, symbol: str, created_at_gte: str | None = None) -> dict:
    """Direct equivalent of rh_mcp.recent_orders — agentic orders for ONE symbol, newest first,
    {"orders": <result>, "errors": {}}."""
    args = {"account_number": account, "symbol": symbol.upper(), "placed_agent": "agentic"}
    if created_at_gte:
        args["created_at_gte"] = created_at_gte
    return _run([("orders", "get_equity_orders", args)])


def review(account: str, spec: dict) -> dict:
    """Direct equivalent of rh_mcp.review — review_equity_order (NO execution; a read tool), returning
    {"review": <result>, "errors": {}}. Python still decides if any alert is blocking; this only fetches
    the raw review payload. place/cancel are NOT in this path and never go direct."""
    return _run([("review", "review_equity_order", {"account_number": account, **spec})])


if __name__ == "__main__":
    import sys
    acct = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AGENTIC_ACCOUNT", "")
    syms = sys.argv[2].split(",") if len(sys.argv) > 2 else None
    t0 = time.time()
    snap = snapshot(acct, syms)
    print(json.dumps(snap, indent=2)[:1200])
    print(f"\n[rh_direct] {time.time() - t0:.2f}s", file=sys.stderr)
