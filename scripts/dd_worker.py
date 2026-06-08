#!/usr/bin/env python3
"""
dd_worker.py — a DETACHED background DD for ONE symbol, for the async (DD_ASYNC) tick.

In async mode the tick dispatches this fire-and-forget instead of blocking ~100s/name on inline
DDs. It runs the SAME run_dd agent (dd_probe quant packet + headless `claude` web research) and
writes the verdict to data/dd_jobs/<SYM>.json. The NEXT tick ingests that file into the DD cache
and acts on it — so DD latency is absorbed across ticks and the tick itself never blocks.

Lifecycle of data/dd_jobs/<SYM>.json:
  {status:"running", ts}                              <- the dispatcher (decide.py) writes this
  {status:"done", ts, ref_price, ref_range_pos, result}  <- THIS worker writes this when finished

The dispatcher owns the "running" marker (so it can skip a symbol already in flight); the worker
only writes the terminal "done". A worker that dies leaves a stale "running" the dispatcher reaps.

Usage (the tick spawns this; rarely run by hand):
    python3 scripts/dd_worker.py SYM --last 1.23 --range-pos 0.9 --intraday 4.5 --reason "..."
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # make sibling modules importable
from decide import run_dd                       # noqa: E402  the SAME agent the tick uses
from investigate import _context_inputs, _candidate_signal  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
JOBS = REPO / "data" / "dd_jobs"


def _write_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Background DD worker for one symbol (async tick).")
    ap.add_argument("symbol")
    ap.add_argument("--last", type=float, default=None)
    ap.add_argument("--range-pos", type=float, default=None)
    ap.add_argument("--intraday", type=float, default=None)
    ap.add_argument("--reason", default="async DD (dispatched)")
    args = ap.parse_args()
    sym = args.symbol.upper().strip()
    job = JOBS / f"{sym}.json"

    regime, caps, portfolio = _context_inputs()
    # Use the signal the tick passed (no re-fetch); fall back to a fresh quote for a manual run.
    sig = {"last": args.last, "intraday_pct": args.intraday, "range_pos": args.range_pos}
    if sig["last"] is None:
        sig = _candidate_signal(sym)
    candidate = {"symbol": sym, "reason": args.reason, **sig}

    try:
        verdict = run_dd(candidate, regime, caps, portfolio, os.environ.get("DD_MODEL", "claude-sonnet-4-6"))
    except Exception as e:  # never leave a half-written job; record the failure so the tick can retry
        verdict = {"symbol": sym, "decision": "error", "error": f"worker_exception: {e}",
                   "conviction": None, "dollar_amount": None, "reason": "", "catalysts": [], "risks": []}

    _write_atomic(job, {"symbol": sym, "status": "done", "ts": time.time(),
                        "ref_price": sig.get("last"), "ref_range_pos": sig.get("range_pos"),
                        "result": verdict})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
