#!/usr/bin/env python3
"""
investigate.py — on-demand deep-DD on ONE named stock, on demand.

It reuses the EXACT same external Claude Code agent the tick uses: scripts/decide.run_dd, which
gathers the dd_probe quant packet and then spawns a headless `claude` web-research call
(WebSearch / WebFetch / get_equity_quotes) to reach a commit/reject verdict. The ONLY difference
from a tick-driven DD is the trigger: you name the symbol instead of the screen surfacing it.

RESEARCH ONLY — it prints the verdict (thesis, sizing, catalysts, risks). It places NO order and
does NOT write the shared DD cache (so a research run can never make the live engine trade). To act
on a name, let the tick screen it or run it through the normal decide -> apply/live path.

Usage:
    python3 scripts/investigate.py TSLA
    python3 scripts/investigate.py TSLA --json     # machine-readable verdict
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # make sibling modules importable
import market_conditions as mc            # noqa: E402  (after sys.path tweak)
from decide import run_dd, reset_usage, usage_summary  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
CONTEXT = REPO / "data" / "tick" / "context_latest.json"


def _candidate_signal(sym: str) -> dict:
    """Fresh quote -> the screen_signal fields run_dd shows the agent (last / intraday% / range_pos).
    Best-effort: run_dd re-probes the name anyway, so a miss here just leaves the signal null."""
    try:
        q = (mc.fetch_cboe([sym.upper()]) or {}).get(sym.upper(), {})
    except Exception:
        q = {}
    return {"last": q.get("last"),
            "intraday_pct": mc.intraday_pct(q),
            "range_pos": mc.range_position(q)}


def _context_inputs() -> tuple[dict, dict, dict]:
    """regime / caps / portfolio from the latest tick context (refreshed every tick). Falls back to
    minimal stand-ins so an investigation works even before the engine has produced a context."""
    try:
        ctx = json.loads(CONTEXT.read_text())
    except (OSError, ValueError):
        ctx = {}
    regime = ctx.get("regime", {})
    caps = ctx.get("caps")
    if not caps:  # no tick has run yet — synthesize sizing context from .env so the agent can reason
        eq = float(os.environ.get("PAPER_START_CASH", "3000"))
        caps = {"MAX_POSITION_USD": round(eq * 0.05, 2), "MAX_TOTAL_EXPOSURE_USD": round(eq * 0.8, 2),
                "MIN_POSITION_USD": 0.0, "STOP_LOSS_PCT": float(os.environ.get("STOP_LOSS_PCT", "8")),
                "TAKE_PROFIT_PCT": float(os.environ.get("TAKE_PROFIT_PCT", "25")),
                "MAX_PER_TRADE_LOSS_USD": round(eq * 0.02, 2),
                "MAX_OPEN_POSITIONS": int(os.environ.get("MAX_OPEN_POSITIONS", "20"))}
    pf = ctx.get("portfolio", {})
    positions = ctx.get("positions", [])
    portfolio = {"cash": pf.get("cash", 0.0), "exposure": pf.get("positions_value", 0.0),
                 "open_positions": pf.get("open_positions", len(positions)),
                 "held": [p.get("symbol") for p in positions]}
    return regime, caps, portfolio


def investigate(sym: str) -> dict:
    """Run the tick's DD agent on one named symbol and return its raw verdict dict (no side effects
    beyond the dd_probe file write run_dd already does)."""
    sym = sym.upper().strip()
    regime, caps, portfolio = _context_inputs()
    candidate = {"symbol": sym, "reason": "manual investigation (on-demand)", **_candidate_signal(sym)}
    dd_model = os.environ.get("DD_MODEL", "claude-sonnet-4-6")
    return run_dd(candidate, regime, caps, portfolio, dd_model)


def _print_verdict(sym: str, v: dict) -> None:
    dec = (v.get("decision") or "?").upper()
    print(f"\n=== DD: {sym} -> {dec} ===")
    if v.get("decision") == "commit":
        print(f"  size ${v.get('dollar_amount')}  conviction={v.get('conviction')}  "
              f"hold={v.get('hold_intent')}  type={v.get('catalyst_type')}")
        trig = v.get("entry_trigger")
        if isinstance(trig, dict) and trig.get("price"):
            print(f"  armed entry: {trig.get('direction')} {trig.get('price')}")
    print(f"  thesis: {(v.get('reason') or '').strip()}")
    if v.get("catalysts"):
        print(f"  catalysts: {', '.join(map(str, v['catalysts']))}")
    if v.get("risks"):
        print(f"  risks: {', '.join(map(str, v['risks']))}")
    if v.get("next_earnings_date"):
        print(f"  next earnings: {v.get('next_earnings_date')}")
    if v.get("never_buy"):
        print(f"  NEVER-BUY: {v.get('never_buy_reason')}")
    if v.get("error"):
        print(f"  error: {v.get('error')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="On-demand deep-DD on one stock (research only).")
    ap.add_argument("symbol")
    ap.add_argument("--json", action="store_true", help="emit the raw verdict dict as JSON")
    args = ap.parse_args()

    reset_usage()  # clean token ledger so we can report this DD's exact cost
    verdict = investigate(args.symbol)
    usage = usage_summary()

    if args.json:
        print(json.dumps({"verdict": verdict, "token_usage": usage}, indent=2))
        return 0
    _print_verdict(args.symbol.upper(), verdict)
    if usage.get("n_calls"):
        print(f"  [cost: {usage['total_tokens']:,} tok ~${usage['cost_usd']:.4f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
