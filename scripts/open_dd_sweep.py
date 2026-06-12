#!/usr/bin/env python3
"""
open_dd_sweep.py — market-open DD burst.

PROBLEM. The screen surfaces the whole morning slate at once — often ~40 net-new movers — but the
steady-state tick deliberately throttles fresh DDs (MAX_DD_CANDIDATES / DD_ASYNC_MAX_INFLIGHT) so
dynamic discovery doesn't re-burn Sonnet+web on the same recurring names every 5 min. At the OPEN
that throttle is backwards: it's a one-time backlog of brand-new names, and gap-drift edge decays
fastest in the first hour. At ~5 DDs/tick a 40-name slate isn't fully evaluated until ~8 ticks
(~40 min) later — by which point the early movers have run.

FIX. Once per morning, drain the ENTIRE candidate list through a bounded pool of background workers
so every name is DD'd within minutes, then hand back to the normal throttled tick for the rest of
the day. This is DISPATCH-ONLY: it spawns the SAME detached dd_worker.py the async tick uses (each
writes data/dd_jobs/<SYM>.json), keeps OPEN_SWEEP_CONCURRENCY workers saturated until the slate
drains, and exits. It never writes the DD cache — decide.py stays the SOLE cache writer; the next
regular tick ingests these verdicts (folding their token spend into its ledger) and acts on them.

SAFE TO RUN ALONGSIDE TICKS. Per-name job files are written atomically and a name already in flight
is skipped, so a tick firing mid-sweep just sees the pool full (count_in_flight >= its own inflight
cap), dispatches nothing, and ingests whatever has finished. No cross-process cache lock needed.

SELF-GATING (TZ-robust, schedule it dumbly on StartInterval). The driver runs at most once per ET
trading day, only inside the open window, only when the freshly-written context allows entries:
  * ET weekday and OPEN_SWEEP_OPEN <= now <= OPEN_SWEEP_LATEST (defaults 09:30..10:30 ET)
  * data/.open_sweep_done-<ET-date> marker absent (written after a run so re-fires no-op)
  * context_latest.json present with allow_entries true, not hostile, and book not full
Holidays/halts need no special case — context.allow_entries is already false, so the sweep exits.

KNOBS (.env):
  OPEN_SWEEP_CONCURRENCY=10     concurrent background workers (the real spend/CPU governor)
  OPEN_SWEEP_MAX=0              cap on names dispatched (0 = all candidates)
  OPEN_SWEEP_DEADLINE_S=720     hard wall-clock stop for the saturating loop
  OPEN_SWEEP_POLL_S=5           re-check cadence while waiting for a worker slot
  OPEN_SWEEP_OPEN=09:30         window start (ET, HH:MM)
  OPEN_SWEEP_LATEST=10:30       window end (ET, HH:MM) — after this, skip (the tick has been chipping)

Usage (launchd fires the wrapper; rarely run by hand):
    python3 scripts/open_dd_sweep.py            # gated run
    python3 scripts/open_dd_sweep.py --force    # ignore window + once-a-day marker (testing)
    python3 scripts/open_dd_sweep.py --dry-run  # log what WOULD dispatch, spawn nothing
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # make sibling modules importable
# Reuse the EXACT async-DD plumbing the tick uses, so the sweep and the tick dispatch identically.
from decide import (  # noqa: E402
    count_in_flight, dispatch_dd, job_in_flight, load_cache, ET,
)

REPO = Path(__file__).resolve().parent.parent
CONTEXT = REPO / "data" / "tick" / "context_latest.json"


def _hhmm(env_key: str, default: str) -> tuple[int, int]:
    raw = os.environ.get(env_key, default).strip()
    try:
        h, m = raw.split(":")
        return int(h), int(m)
    except ValueError:
        h, m = default.split(":")
        return int(h), int(m)


def _marker_path(et_date: str) -> Path:
    return REPO / "data" / f".open_sweep_done-{et_date}"


def _within_window(now_et: datetime) -> tuple[bool, str]:
    """True if now (ET) is a weekday inside [OPEN_SWEEP_OPEN, OPEN_SWEEP_LATEST]."""
    if now_et.weekday() >= 5:
        return False, "weekend"
    oh, om = _hhmm("OPEN_SWEEP_OPEN", "09:30")
    lh, lm = _hhmm("OPEN_SWEEP_LATEST", "10:30")
    cur = now_et.hour * 60 + now_et.minute
    if cur < oh * 60 + om:
        return False, f"before open window ({oh:02d}:{om:02d} ET)"
    if cur > lh * 60 + lm:
        return False, f"past open window (>{lh:02d}:{lm:02d} ET)"
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Market-open DD burst — drain the full slate concurrently.")
    ap.add_argument("--force", action="store_true", help="ignore ET window + once-a-day marker")
    ap.add_argument("--dry-run", action="store_true", help="log intended dispatches, spawn nothing")
    ap.add_argument("--gate-only", action="store_true",
                    help="check ONLY the ET window + once-a-day marker (no context/network). "
                         "Exit 0 if the sweep would proceed, 10 if it should skip — lets the "
                         "wrapper avoid refreshing data outside the open window.")
    args = ap.parse_args()

    now_et = datetime.now(ET)
    et_date = now_et.strftime("%Y-%m-%d")
    stamp = now_et.strftime("%H:%M:%S")

    if not args.force:
        ok, why = _within_window(now_et)
        if not ok:
            print(f"[{stamp}] open-sweep: skip — {why}")
            return 10 if args.gate_only else 0
        if _marker_path(et_date).exists():
            print(f"[{stamp}] open-sweep: skip — already swept {et_date}")
            return 10 if args.gate_only else 0

    if args.gate_only:
        print(f"[{stamp}] open-sweep: in window, not yet swept — proceed")
        return 0

    try:
        ctx = json.loads(CONTEXT.read_text())
    except (OSError, ValueError) as e:
        print(f"[{stamp}] open-sweep: no context_latest.json ({e}) — wrapper should run tick_context first")
        return 0

    caps = ctx.get("caps") or {}
    screen = ctx.get("screen", {})
    candidates = screen.get("entry_candidates") or []
    pf = ctx.get("portfolio", {})
    exposure = pf.get("positions_value", 0.0)
    cash = pf.get("cash", 0.0)

    def _stop(reason: str) -> int:
        # Mark done on a clean structural no-op so we don't re-attempt every poll all morning.
        # (Missing-context above returns WITHOUT marking — that's transient; let the next fire retry.)
        print(f"[{stamp}] open-sweep: nothing to do — {reason}")
        if not args.force and not args.dry_run:
            _marker_path(et_date).write_text(f"{reason}\n")
        return 0

    if not ctx.get("allow_entries"):
        return _stop(f"allow_entries=false (gate={ctx.get('gate')})")
    if screen.get("hostile_regime"):
        return _stop("hostile regime")
    if not candidates:
        return _stop("no entry candidates")

    # Book-full short-circuit (mirrors decide.py): if there's no room for even a min lot, DDing the
    # slate is pure wasted spend — every verdict would reject on the portfolio cap, not the name.
    headroom = max(0.0, min(caps.get("MAX_POSITION_USD", 0.0),
                            caps.get("MAX_TOTAL_EXPOSURE_USD", 0.0) - exposure,
                            cash))
    min_headroom = float(os.environ.get("MIN_ENTRY_HEADROOM_USD", caps.get("MIN_POSITION_USD") or 25.0))
    if headroom < min_headroom:
        return _stop(f"book full (${headroom:.0f} headroom < ${min_headroom:.0f} lot)")

    # Build the pending slate: skip names already held, already cached today (the sweep is for the
    # NET-NEW backlog — a name with today's verdict is served free by the tick), or already in flight.
    held = {str(s).upper() for s in (pf.get("held") or [p.get("symbol") for p in ctx.get("positions", [])])}
    cache = load_cache()
    now = time.time()
    cap = int(os.environ.get("OPEN_SWEEP_MAX", "0"))   # 0 = all
    pending: list[tuple[str, dict]] = []
    skipped_cached = skipped_held = skipped_inflight = 0
    for c in candidates:
        sym = str(c.get("symbol", "")).upper().strip()
        if not sym:
            continue
        if sym in held:
            skipped_held += 1
            continue
        cached = cache.get(sym) or {}
        if (cached.get("result") or {}).get("decision") in ("commit", "reject"):
            skipped_cached += 1
            continue
        if job_in_flight(sym, now):
            skipped_inflight += 1
            continue
        pending.append((sym, c))
    if cap > 0:
        pending = pending[:cap]

    print(f"[{stamp}] open-sweep: {len(candidates)} candidates -> {len(pending)} to dispatch "
          f"(skipped {skipped_cached} cached, {skipped_held} held, {skipped_inflight} in-flight)"
          + (f", cap={cap}" if cap else ""))
    if not pending:
        return _stop("all candidates already cached/held/in-flight")

    concurrency = int(os.environ.get("OPEN_SWEEP_CONCURRENCY", "10"))
    deadline_s = float(os.environ.get("OPEN_SWEEP_DEADLINE_S", "720"))
    poll_s = float(os.environ.get("OPEN_SWEEP_POLL_S", "5"))
    deadline = time.time() + deadline_s

    if args.dry_run:
        print(f"[{stamp}] open-sweep DRY-RUN: would dispatch (pool={concurrency}): "
              f"{', '.join(s for s, _ in pending)}")
        return 0

    # Saturating dispatch: keep `concurrency` workers running until the slate drains. Each pass tops
    # the pool back up as workers finish; between passes we wait poll_s. count_in_flight() reads the
    # job-file markers — counting OUR workers and any the tick left running — so the pool ceiling is a
    # true global concurrency cap, not just a per-sweep one.
    dispatched: list[str] = []
    while pending and time.time() < deadline:
        now = time.time()
        progressed = False
        while pending and count_in_flight(now) < concurrency:
            sym, c = pending.pop(0)
            if job_in_flight(sym, now):   # a tick grabbed it between our snapshot and now
                continue
            if dispatch_dd(sym, c, now):
                dispatched.append(sym)
                progressed = True
            now = time.time()
        if pending:
            if not progressed:
                # Pool is saturated (cap reached) — wait for a slot before trying again.
                time.sleep(poll_s)
            else:
                # We just filled the pool; brief breath so markers settle before the next count.
                time.sleep(min(poll_s, 1.0))

    leftover = len(pending)
    print(f"[{stamp}] open-sweep: dispatched {len(dispatched)} "
          f"({', '.join(dispatched) or '-'}); {leftover} undispatched"
          + (" (deadline hit)" if leftover else "")
          + f"; {count_in_flight(time.time())} workers running — verdicts ingested by the next tick")
    if not args.force:
        _marker_path(et_date).write_text(
            f"dispatched={len(dispatched)} leftover={leftover} at {stamp} ET\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
