#!/usr/bin/env python3
"""
broker_snapshot.py — LIVE-only pre-step. Pulls the real broker state (buying power, open positions,
recent agentic orders, a few quotes) via the MCP relay and writes data/tick/broker_snapshot.json for
tick_context.py + live_execute.py to consume. NEVER runs in paper mode.

Fail-closed: if the snapshot can't be fetched (MCP down, auth lost, agent timeout), it writes nothing
and exits non-zero so run_trading_tick.sh gates the tick to SKIP — the engine never trades blind on a
stale/absent view of the account.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import rh_mcp

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "tick" / "broker_snapshot.json"


def main() -> int:
    if (os.environ.get("TRADING_MODE", "paper").strip().lower()) != "live":
        print("[broker_snapshot] not live mode — nothing to do", file=sys.stderr)
        return 0

    # No quote step: held-position marks come from tick_context's public (Cboe) quotes, merged into
    # the broker view in live_execute. Quoting the pins here was redundant AND the wrong symbol set —
    # it left our actual holdings unmarked (marked at cost), corrupting equity/day-P&L. So the snapshot
    # now fetches ONLY broker truth (buying power + positions + open orders) — lighter and faster.
    try:
        snap = rh_mcp.snapshot()
    except Exception as e:  # noqa: BLE001 — any failure must fail closed, never trade blind
        print(f"[broker_snapshot] FATAL: snapshot failed: {e}", file=sys.stderr)
        return 2
    if not isinstance(snap, dict) or snap.get("portfolio") is None:
        print("[broker_snapshot] FATAL: snapshot missing/incomplete (no portfolio) — failing closed",
              file=sys.stderr)
        return 2

    snap["_fetched_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snap, indent=2))
    os.replace(tmp, OUT)
    n_pos = len(snap.get("positions") or []) if isinstance(snap.get("positions"), list) else "?"
    print(f"[broker_snapshot] wrote {OUT.name} (positions={n_pos})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
