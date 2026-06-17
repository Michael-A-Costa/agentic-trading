#!/usr/bin/env python3
"""test_open_gate.py — truth-table tests for tick_context.open_window_extension_block.

Pins the opening-window extension gate (entry_timing_replay.py, JBL post-mortem 2026-06-17):
fire only for a name that is BOTH inside the opening window AND at/over the extension cap, and
stay inert when either knob is <=0. Pure helper, so no context fixture needed.

Run:  python3 scripts/test_open_gate.py     (exits non-zero on first failure)
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tick_context import open_window_extension_block as blk  # noqa: E402

_passed = 0


def check(name: str, cond: bool, extra: object = "") -> None:
    global _passed
    if not cond:
        print(f"FAIL: {name} {extra}")
        raise SystemExit(1)
    _passed += 1


# defaults under test: window 60m, cap 6%
W, X = 60.0, 6.0

# --- fires: extended AND in the opening window ---
check("jbl-pattern blocks (+9.2% @ 6m)", blk(9.2, 6.0, W, X) is True)
check("at-cap blocks (=6% @ 30m)", blk(6.0, 30.0, W, X) is True)
check("at-open edge blocks (@ 0m)", blk(7.0, 0.0, W, X) is True)

# --- passes: under the cap, regardless of timing ---
check("mild move passes in window (+3% @ 5m)", blk(3.0, 5.0, W, X) is False)
check("sweet-spot passes (+5.9% @ 10m)", blk(5.9, 10.0, W, X) is False)

# --- passes: extended but OUTSIDE the window (the profit centre — leave it alone) ---
check("extended-but-late passes (+12% @ 90m)", blk(12.0, 90.0, W, X) is False)
check("window edge is exclusive (@ 60m)", blk(20.0, 60.0, W, X) is False)

# --- inert / guard rails ---
check("None intraday never blocks", blk(None, 5.0, W, X) is False)
check("None mins (closed/odd) never blocks", blk(9.0, None, W, X) is False)
check("zero window disables", blk(9.0, 5.0, 0.0, X) is False)
check("zero cap disables", blk(9.0, 5.0, W, 0.0) is False)
check("negative mins (pre-open) never blocks", blk(9.0, -3.0, W, X) is False)

print(f"ok — {_passed} checks passed")
