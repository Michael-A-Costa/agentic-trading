#!/usr/bin/env python3
"""
test_live_tick_context.py — dependency-free unit tests for the LIVE context gatherer's state
bridge (live_tick_context.load_live_state). This builds the paper-state-shaped dict that the
shared context builder gates on, so a dropped field here silently disables a whole gate live-side.

Regression pinned: the post-exit re-entry cooldown. live_execute stamps `last_exit` into
live_state.json on every sell, but the cooldown gate lives in the shared builder and reads it off
the state dict. load_live_state must FORWARD last_exit — without it the gate's `cooling` set is
always empty live (it worked paper-side, which reads paper_state.json directly), and a name we
just sold (e.g. ALOY, sold then re-committed same session) re-enters inside its cooldown window.

Run:  python3 scripts/test_live_tick_context.py     (exits non-zero on first failure)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_tick_context as ltc  # noqa: E402
import live_snapshot as ls       # noqa: E402

_passed = 0


def check(name: str, cond: bool, extra: object = "") -> None:
    global _passed
    if not cond:
        print(f"FAIL: {name} {extra}")
        raise SystemExit(1)
    _passed += 1


def _with_fixtures(tmp: Path, live_state: dict, monkeypatch_parsers: bool = True) -> dict:
    """Point ltc.DATA at a temp dir holding a live_state.json fixture, stub the broker-snapshot
    parser (we're testing the state bridge, not snapshot parsing), and return load_live_state()."""
    (tmp / "tick").mkdir(parents=True, exist_ok=True)
    (tmp / "tick" / "broker_snapshot.json").write_text("{}")
    (tmp / "live_state.json").write_text(json.dumps(live_state))
    orig_data, orig_port, orig_pos = ltc.DATA, ls.parse_portfolio, ls.parse_positions
    try:
        ltc.DATA = tmp
        if monkeypatch_parsers:
            ls.parse_portfolio = lambda *_: {"cash": 1000.0}
            ls.parse_positions = lambda *_: {}
        return ltc.load_live_state()
    finally:
        ltc.DATA, ls.parse_portfolio, ls.parse_positions = orig_data, orig_port, orig_pos


def test_forwards_last_exit(tmp: Path) -> None:
    # the core regression: last_exit on live_state.json must reach the returned state dict so the
    # shared builder's COOLDOWN_MIN gate can populate its `cooling` set live-side.
    ls_fixture = {"day": "2026-06-12", "start_of_day_equity": 5000.0, "lots": {},
                  "last_exit": {"ALOY": "2026-06-12T13:50:29+00:00"}}
    st = _with_fixtures(tmp, ls_fixture)
    check("last_exit key present", "last_exit" in st, st.keys())
    check("ALOY stamp forwarded", st["last_exit"].get("ALOY") == "2026-06-12T13:50:29+00:00", st)


def test_last_exit_defaults_to_empty_dict(tmp: Path) -> None:
    # a live_state.json with no last_exit (fresh account / pre-first-sell) must yield {}, never None
    # or a missing key — the builder does `(state.get("last_exit") or {}).items()` but the contract
    # should hold regardless so the gate can't KeyError or skip.
    st = _with_fixtures(tmp, {"day": "2026-06-12", "lots": {}})
    check("last_exit present even when absent upstream", "last_exit" in st, st.keys())
    check("last_exit defaults to {}", st["last_exit"] == {}, st["last_exit"])


def test_cooldown_gate_filters_a_just_sold_name(tmp: Path) -> None:
    """End-to-end on the gate logic the bridge feeds: a name exited 1h ago is inside a 24h cooldown
    -> lands in `cooling`; a name exited 2d ago is past it -> not. Mirrors tick_context's cooling
    block exactly so the bridge's output is proven to drive the actual entry filter, not just exist.
    """
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(minutes=60)).isoformat(timespec="seconds")
    stale = (now - timedelta(days=2)).isoformat(timespec="seconds")
    st = _with_fixtures(tmp, {"lots": {}, "last_exit": {"ALOY": recent, "OLDX": stale}})
    cooldown_min = 1440.0
    cooling = set()
    for s, ts in (st.get("last_exit") or {}).items():
        if (now - datetime.fromisoformat(ts)).total_seconds() / 60.0 < cooldown_min:
            cooling.add(s)
    check("recently-sold name is cooling", "ALOY" in cooling, cooling)
    check("long-ago-sold name is NOT cooling", "OLDX" not in cooling, cooling)


if __name__ == "__main__":
    import tempfile
    tests = [test_forwards_last_exit, test_last_exit_defaults_to_empty_dict,
             test_cooldown_gate_filters_a_just_sold_name]
    for fn in tests:
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
    print(f"OK — {_passed} assertions passed across {len(tests)} tests")
