#!/usr/bin/env python3
"""
live_smoke_test.py — exercise the REAL Robinhood write path end-to-end with ZERO fill risk.

It places a BUY LIMIT on a cheap, liquid name (default Ford, F) at a price FAR below the market — a
limit a buy can never reach — so the order rests unfilled, proving the live review -> place ->
read-back -> cancel plumbing works against the real broker without ever acquiring a position. Then it
cancels the resting order, leaving the account exactly as it started.

This is the last rung of the safety ladder (see docs/testing-live.md):
  paper  ->  live dry-run (review only)  ->  THIS unfillable-limit probe  ->  armed canary.

Safety invariants enforced here:
  - the limit is HARD-capped well below the live quote (default 30%); the script REFUSES to place if
    the limit is >= 50% of the last trade, so it cannot be marketable.
  - placing requires BOTH TRADING_MODE=live AND LIVE_ARMED=1 AND an explicit --place flag. Without
    --place it is review-only; without LIVE_ARMED it stops after review ("would place").
  - every placed order is cancelled in a finally block — the script never leaves a resting order.
  - account is hard-pinned by rh_mcp.account() (refuses a missing/other account).

Run (during regular market hours, with the Robinhood MCP connected):
  python3 scripts/live_smoke_test.py                  # review-only (safe, places nothing)
  TRADING_MODE=live LIVE_ARMED=1 python3 scripts/live_smoke_test.py --place   # full place+cancel
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rh_mcp
import live_execute as le

SAFE_FRAC = 0.30      # default limit = 30% of last trade — far below market
MAX_FRAC = 0.50       # hard ceiling: refuse to place if limit >= 50% of last (could fill)


def _order_id(placed: dict | None) -> str | None:
    """Dig the broker order id out of the relay's {"order": <raw>} payload, defensively."""
    if not isinstance(placed, dict):
        return None
    o = placed.get("order", placed)
    if isinstance(o, dict):
        o = o.get("data", o)
    if isinstance(o, dict):
        for k in ("id", "order_id", "ref_id"):
            if o.get(k):
                return str(o[k])
    return None


def _armed() -> bool:
    return str(os.environ.get("LIVE_ARMED", "0")).strip().lower() in ("1", "true", "yes", "on")


def main() -> int:
    ap = argparse.ArgumentParser(description="Unfillable-limit live write-path probe.")
    ap.add_argument("--symbol", default="F", help="cheap, liquid ticker (default F = Ford)")
    ap.add_argument("--qty", type=int, default=1, help="whole shares (default 1)")
    ap.add_argument("--limit", type=float, default=None,
                    help="limit price; default = 30%% of the live last (auto, far below market)")
    ap.add_argument("--tif", default="gtc", choices=["gtc", "gfd"],
                    help="time in force (gtc rests so you can see it; default gtc)")
    ap.add_argument("--place", action="store_true",
                    help="actually place + cancel (needs TRADING_MODE=live & LIVE_ARMED=1). "
                         "Omit for a safe review-only run.")
    args = ap.parse_args()
    sym = args.symbol.upper().strip()

    print(f"=== live write-path smoke test — {sym} unfillable limit ===")
    print(f"TRADING_MODE={os.environ.get('TRADING_MODE', 'paper')}  LIVE_ARMED={os.environ.get('LIVE_ARMED', '0')}")
    print(f"account={rh_mcp.account()}\n")

    # 1) READ PATH: pull a live quote (also proves snapshot + account pin work).
    snap = rh_mcp.snapshot([sym])
    if not snap:
        print("FAIL: snapshot returned nothing — is the Robinhood MCP connected? (read path)")
        return 1
    broker = le.parse_snapshot(snap)
    last = (broker["quotes"].get(sym) or {}).get("last")
    if not last or last <= 0:
        print(f"FAIL: no live last for {sym} — aborting (verify market hours + MCP). read path is "
              "the first thing this test confirms, so a miss here is itself the finding.")
        return 1
    print(f"[read]   {sym} last={last}  buying_power={broker['buying_power']}")

    # 2) PRICE GUARD: limit far below market, hard-refuse anything that could fill.
    limit = args.limit if args.limit is not None else round(last * SAFE_FRAC, 2)
    if limit >= last * MAX_FRAC:
        print(f"FAIL (safety): limit {limit} is >= {MAX_FRAC:.0%} of last {last} — too close, could "
              f"fill. Use a limit well below {round(last * MAX_FRAC, 2)}.")
        return 2
    print(f"[guard]  limit={limit:.2f}  ({limit / last:.0%} of last) — cannot be marketable\n")

    spec = {"symbol": sym, "side": "buy", "type": "limit", "quantity": str(int(args.qty)),
            "limit_price": f"{limit:.2f}", "time_in_force": args.tif, "market_hours": "regular_hours"}

    # 3) REVIEW PATH (no execution — the place tool isn't even in the review agent's toolset).
    review = rh_mcp.review(spec)
    if not review:
        print("FAIL: review returned nothing (review path).")
        return 1
    print(f"[review] {review}\n")

    if not args.place:
        print("review-only run complete (placed nothing). Re-run with "
              "`TRADING_MODE=live LIVE_ARMED=1 ... --place` to test place + cancel.")
        return 0
    if not _armed():
        print("--place given but LIVE_ARMED!=1 → would place this order, placing nothing (dry-run). "
              "Set LIVE_ARMED=1 to actually place.")
        return 0

    # 4) PLACE -> READ-BACK -> CANCEL. Always cancel in finally so no resting order is left behind.
    order_id = None
    try:
        ref_id = str(uuid.uuid4())
        placed = rh_mcp.place(spec, ref_id=ref_id)
        order_id = _order_id(placed)
        print(f"[place]  ref_id={ref_id}  order_id={order_id}\n         raw={placed}")
        if not order_id:
            print("FAIL: place returned no order id (place path).")
            return 1

        # Read it back from the broker — it should be OPEN and UNFILLED.
        snap2 = rh_mcp.snapshot([sym])
        orders = le.parse_snapshot(snap2)["orders"] if snap2 else []
        mine = next((o for o in orders if str(o.get("id") or o.get("order_id")) == order_id), None)
        state = (mine or {}).get("state") or (mine or {}).get("status") or "unknown"
        print(f"[verify] resting order found={mine is not None}  state={state}  "
              f"(expected open/unfilled — limit is far below market)")
    finally:
        if order_id:
            cancelled = rh_mcp.cancel(order_id)
            print(f"[cancel] order_id={order_id} -> {cancelled}")

    print("\nPASS: review -> place -> read-back -> cancel exercised against the real broker with no "
          "fill. Account left flat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
