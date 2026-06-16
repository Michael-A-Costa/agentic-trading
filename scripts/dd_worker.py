#!/usr/bin/env python3
"""
dd_worker.py — a DETACHED background DD for ONE symbol, for the async (DD_ASYNC) tick.

In async mode the tick dispatches this fire-and-forget instead of blocking ~100s/name on inline
DDs. It runs the SAME run_dd agent (dd_probe quant packet + headless `claude` web research) and
writes the verdict to data/dd_jobs/<SYM>.json. A finished COMMIT then forces an out-of-band tick
(debounced + cooldown-capped, like the manage wave) so the buy is acted on within seconds instead
of waiting for the next scheduled tick; rejects just ride the cache. DD latency is absorbed across
ticks and the tick itself never blocks.

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
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # make sibling modules importable
from decide import run_dd, usage_summary        # noqa: E402  the SAME agent the tick uses
from investigate import _context_inputs, _candidate_signal  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
JOBS = REPO / "data" / "dd_jobs"
TRIGGER_LOCK = REPO / "data" / ".dd_trigger.lock"   # serializes the debounce -> ONE trigger per burst
TRIGGER_LAST = REPO / "data" / ".dd_trigger.last"   # last forced-tick ts -> cooldown ceiling


def _write_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


def _tick_script() -> Path | None:
    """The entry script for THIS tick's mode (inherited via TRADING_MODE), if it exists."""
    mode = (os.environ.get("TRADING_MODE") or "paper").lower()
    p = REPO / "scripts" / ("run_live_tick.sh" if mode == "live" else "run_paper_tick.sh")
    return p if p.exists() else None


def _maybe_trigger_tick(actionable: bool) -> None:
    """Force an out-of-band tick to INGEST + act on this burst's entry verdicts now, instead of
    waiting for the next scheduled tick. Mirror of manage_worker._maybe_trigger_tick — same three
    guards, dedicated lock/last files so an entry burst and a manage burst coalesce independently:
      • coalesce — only ONE worker in a finishing burst becomes the trigger (atomic mkdir on a lock);
        the rest return, and their done-files (already on disk) ride along into the trigger's tick.
      • cooldown — skip if a tick was forced < DD_ASYNC_TRIGGER_COOLDOWN_S ago, so a finishing burst
        (incl. the open sweep's) collapses to at most one forced tick and can't chain into a loop.
      • actionable-only — only a 'commit' is urgent (there's a buy to act on); a reject just rides
        the cache until the next scheduled tick, so it never forces a tick.
    The forced tick self-guards with the shared .tick.lock, so it can never run concurrently with the
    scheduled tick (or a manage-forced tick) — a collision just skips."""
    if not actionable:
        return
    if os.environ.get("DD_ASYNC_TRIGGER_TICK", "1").strip().lower() not in ("1", "true", "yes"):
        return
    script = _tick_script()
    if script is None:
        return
    # Become the sole trigger for this burst (atomic mkdir); reclaim a stale lock from a dead trigger.
    try:
        TRIGGER_LOCK.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(TRIGGER_LOCK)
    except FileExistsError:
        try:
            if time.time() - TRIGGER_LOCK.stat().st_mtime > 60:
                os.rmdir(TRIGGER_LOCK); os.mkdir(TRIGGER_LOCK)   # reclaim + take it
            else:
                return                                            # another worker is already debouncing
        except OSError:
            return
    except OSError:
        return
    try:
        cooldown = float(os.environ.get("DD_ASYNC_TRIGGER_COOLDOWN_S", "90"))
        try:
            last = float(TRIGGER_LAST.read_text().strip())
        except (OSError, ValueError):
            last = 0.0
        if time.time() - last < cooldown:
            return  # a tick was forced very recently -> let the verdict wait for the next tick
        debounce = float(os.environ.get("DD_ASYNC_TRIGGER_DEBOUNCE_S", "6"))
        time.sleep(max(0.0, debounce))  # let the rest of the burst finish + write their done-files
        try:
            TRIGGER_LAST.write_text(str(time.time()))
        except OSError:
            pass
        subprocess.Popen(["/usr/bin/env", "bash", str(script)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)  # detached; self-guards with .tick.lock
    finally:
        try:
            os.rmdir(TRIGGER_LOCK)
        except OSError:
            pass


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

    # This worker is a detached process with its OWN in-process token ledger; usage_summary() here
    # captures exactly this symbol's DD spend. Persist it so the ingesting tick can fold it into its
    # TOKENS line (otherwise async DD cost is incurred but never measured/logged).
    _write_atomic(job, {"symbol": sym, "status": "done", "ts": time.time(),
                        "ref_price": sig.get("last"), "ref_range_pos": sig.get("range_pos"),
                        "usage": usage_summary(),
                        "result": verdict})

    # Verdict is on disk -> if it's a commit, force an out-of-band tick to ingest + buy it now
    # (debounced + cooldown-capped so a finishing burst collapses to ONE tick and can't loop).
    _maybe_trigger_tick(actionable=verdict.get("decision") == "commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
