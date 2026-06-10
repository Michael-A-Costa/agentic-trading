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
import time
from datetime import datetime, timezone
from pathlib import Path

import rh_mcp

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "tick" / "broker_snapshot.json"


def _try_snapshot() -> dict | None:
    """One snapshot attempt. Returns a complete snapshot dict, or None if the relay failed OR came
    back without a portfolio (a single flaky get_portfolio nulls the whole view)."""
    try:
        snap = rh_mcp.snapshot()
    except Exception as e:  # noqa: BLE001 — any failure is a miss; caller retries / fails closed
        print(f"[broker_snapshot] attempt failed: {e}", file=sys.stderr)
        return None
    if not isinstance(snap, dict) or snap.get("portfolio") is None:
        print("[broker_snapshot] attempt incomplete (no portfolio)", file=sys.stderr)
        return None
    return snap


def main() -> int:
    if (os.environ.get("TRADING_MODE", "paper").strip().lower()) != "live":
        print("[broker_snapshot] not live mode — nothing to do", file=sys.stderr)
        return 0

    # No quote step: held-position marks come from tick_context's public (Cboe) quotes, merged into
    # the broker view in live_execute. Quoting the pins here was redundant AND the wrong symbol set —
    # it left our actual holdings unmarked (marked at cost), corrupting equity/day-P&L. So the snapshot
    # now fetches ONLY broker truth (buying power + positions + open orders) — lighter and faster.
    #
    # Retry before failing closed: the snapshot is a single point of failure each tick — one transient
    # MCP hiccup on get_portfolio (auth blip / rate-limit / 5xx) nulls the whole view and skips the
    # tick (seen 2026-06-09 13:34). One quick retry rides out that class of one-call flake while
    # keeping the fail-closed guarantee: if EVERY attempt is incomplete, we still write nothing and
    # exit non-zero. Tunable via RH_SNAPSHOT_RETRIES / RH_SNAPSHOT_RETRY_BACKOFF_S.
    attempts = max(1, int(os.environ.get("RH_SNAPSHOT_RETRIES", "2")))
    backoff = float(os.environ.get("RH_SNAPSHOT_RETRY_BACKOFF_S", "5"))
    snap = None
    for i in range(attempts):
        snap = _try_snapshot()
        if snap is not None:
            break
        if i < attempts - 1:
            print(f"[broker_snapshot] retrying in {backoff:.0f}s ({i + 1}/{attempts - 1})",
                  file=sys.stderr)
            time.sleep(backoff)
    if snap is None:
        print(f"[broker_snapshot] FATAL: snapshot missing/incomplete after {attempts} attempt(s) "
              "(no portfolio) — failing closed", file=sys.stderr)
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
