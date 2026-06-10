#!/usr/bin/env python3
"""
state_lock.py — a short-lived mkdir lock serializing every paper_state.json read-modify-write across
the two-rate loop (see docs/two-rate-architecture.md): the 5-min planner's executor (apply_decision),
the 1-min sentinel, and the start-of-day-equity rollover in tick_context.

It is held ONLY around a state mutation — milliseconds — NEVER during the planner's DD or the
sentinel's quote fetch. That is the whole point: the slow loop no longer blocks the fast loop. The
old design shared the coarse data/.tick.lock (held for the planner's entire ~minutes-long tick), so
a DD-heavy tick starved the sentinel; this replaces that with a critical section around just the
read→mutate→write.

mkdir is atomic on POSIX. A lock older than `stale` is reclaimed (a crashed/killed holder). If the
lock still can't be taken within `timeout`, we fail OPEN (proceed without it) rather than skip a
protective exit — both writers re-read state fresh, so the worst case is a rare interleaved write,
which is strictly better than a missed stop. `timeout > stale` so a hung holder is reclaimed before
we ever give up.
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

LOCK = Path(__file__).resolve().parent.parent / "data" / ".state.lock"


@contextmanager
def state_lock(timeout: float = 20.0, stale: float = 15.0, poll: float = 0.02):
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()
    held = False
    while True:
        try:
            os.mkdir(LOCK)
            held = True
            break
        except FileExistsError:
            try:
                if time.time() - LOCK.stat().st_mtime > stale:
                    os.rmdir(LOCK)            # reclaim a crashed holder, then retry
                    continue
            except OSError:
                pass
            if time.time() - start > timeout:
                sys.stderr.write("[state_lock] timeout — proceeding WITHOUT the lock (fail-open)\n")
                break
            time.sleep(poll)
    try:
        yield
    finally:
        if held:
            try:
                os.rmdir(LOCK)
            except OSError:
                pass
