#!/usr/bin/env python3
"""
manage_worker.py — a DETACHED background Tier-2 manage-DD for ONE held position (MANAGE_ASYNC tick).

Mirror of dd_worker.py for the manage wave. In async mode the tick spawns this fire-and-forget
instead of BLOCKING ~20s on the inline Haiku/Sonnet news judgment. It runs the SAME run_manage_dd
the tick uses (dd_probe quant refresh + headless `claude` news read) on the risk-scored position
packet the dispatcher froze into the 'running' marker, and writes the verdict to
data/manage_jobs/<SYM>.json. The NEXT tick ingests that file and APPLIES the keep/trim/exit/add
against the live position — so manage latency is absorbed across ticks and the tick never blocks.

Lifecycle of data/manage_jobs/<SYM>.json:
  {status:"running", ts, arm, model, band, ttl_min, input:{position, regime, caps, portfolio}}  <- dispatcher (decide.py)
  {status:"done", ts, band, ttl_min, usage, result}                                             <- THIS worker

The dispatcher owns the 'running' marker (so it can skip a holding already in flight); the worker
reads its frozen input from that marker, then overwrites it with the terminal 'done'. A worker that
dies leaves a stale 'running' the dispatcher reaps after MANAGE_ASYNC_RUNNING_TIMEOUT_S.

Usage (the tick spawns this; rarely run by hand):
    python3 scripts/manage_worker.py SYM
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # make sibling modules importable
from decide import run_manage_dd, reset_usage, usage_summary  # noqa: E402  the SAME agent the tick uses

REPO = Path(__file__).resolve().parent.parent
JOBS = REPO / "data" / "manage_jobs"
TRIGGER_LOCK = REPO / "data" / ".manage_trigger.lock"   # serializes the debounce -> ONE trigger per burst
TRIGGER_LAST = REPO / "data" / ".manage_trigger.last"   # last forced-tick ts -> cooldown ceiling


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
    """Force an out-of-band tick to APPLY this burst's verdicts now, instead of waiting for the next
    scheduled tick. Three guards keep it safe:
      • coalesce — only ONE worker in a finishing burst becomes the trigger (atomic mkdir on a lock);
        the rest return, and their done-files (already on disk) ride along into the trigger's tick.
      • cooldown — skip if a tick was forced < MANAGE_ASYNC_TRIGGER_COOLDOWN_S ago, so a forced tick
        re-dispatching an always-due (critical) holding can't chain into a runaway loop.
      • actionable-only — a 'keep' verdict isn't urgent (cache TTL coasts), so it never forces a tick.
    The forced tick self-guards with the shared .tick.lock, so it can never run concurrently with the
    scheduled tick — a collision just skips."""
    if not actionable:
        return
    if os.environ.get("MANAGE_ASYNC_TRIGGER_TICK", "1").strip().lower() not in ("1", "true", "yes"):
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
        cooldown = float(os.environ.get("MANAGE_ASYNC_TRIGGER_COOLDOWN_S", "90"))
        try:
            last = float(TRIGGER_LAST.read_text().strip())
        except (OSError, ValueError):
            last = 0.0
        if time.time() - last < cooldown:
            return  # a tick was forced very recently -> let the verdict wait for the next tick
        debounce = float(os.environ.get("MANAGE_ASYNC_TRIGGER_DEBOUNCE_S", "6"))
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
    if len(sys.argv) < 2:
        sys.stderr.write("usage: manage_worker.py SYM\n")
        return 2
    sym = sys.argv[1].upper().strip()
    job = JOBS / f"{sym}.json"
    try:
        spec = json.loads(job.read_text())
    except (OSError, ValueError) as e:
        sys.stderr.write(f"[manage_worker] {sym}: no/invalid running marker ({e})\n")
        return 1
    inp = spec.get("input") or {}
    model = spec.get("model") or os.environ.get("DD_MODEL_MANAGE", "claude-haiku-4-5-20251001")

    reset_usage()   # this detached process keeps its OWN token ledger -> exactly this holding's spend
    try:
        res = run_manage_dd(inp.get("position") or {"symbol": sym},
                            inp.get("regime") or {}, inp.get("caps") or {},
                            inp.get("portfolio") or {}, model)
    except Exception as e:  # never leave a half-written job; record the failure so the tick keeps the lot
        res = {"symbol": sym, "action": "keep", "error": f"manage_worker_exception: {e}",
               "reason": "manage worker failed -> keep (stop + Tier-1 still protect)"}
    # Carry the A/B arm + model through onto the verdict so the ingesting tick can attribute the
    # exit/trim to the right arm on the trade row (matches the synchronous path).
    res["manage_arm"], res["manage_model"] = spec.get("arm"), spec.get("model")

    # band/ttl_min ride through unchanged so the ingest stamps the manage cache with the risk snapshot
    # that made this holding due (the tick coasts its TTL off that, exactly as the sync path does).
    _write_atomic(job, {"symbol": sym, "status": "done", "ts": time.time(),
                        "band": spec.get("band"), "ttl_min": spec.get("ttl_min"),
                        "usage": usage_summary(), "result": res})

    # Verdict is on disk -> if it's actionable, force an out-of-band tick to apply it now (debounced +
    # cooldown-capped so a finishing burst collapses to ONE tick and can't chain into a loop).
    _maybe_trigger_tick(actionable=res.get("action") in ("exit", "trim", "add"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
