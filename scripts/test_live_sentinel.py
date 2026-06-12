#!/usr/bin/env python3
"""Unit tests for live_sentinel's detection layer — the quote-key regression that silently killed
breach detection, and the 1-min tier-trim watch (2026-06-11)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_sentinel as lsn  # noqa: E402

_passed = 0


def check(name: str, cond: bool, extra: object = "") -> None:
    global _passed
    if not cond:
        print(f"FAIL: {name} {extra}")
        raise SystemExit(1)
    _passed += 1


def _patch_quotes(monkey_prices, rh_batch=None):
    """Patch the quote layer: rh_direct batch returns rh_batch (default: nothing -> Cboe fallback),
    dd_probe.cboe_quote pops raw-Cboe-shaped quotes, and the confirm sleep is neutered."""
    import dd_probe
    seq = list(monkey_prices)

    def fake_quote(sym):
        px = seq.pop(0) if len(seq) > 1 else seq[0]
        return {"current_price": px, "bid": px - 0.02, "ask": px + 0.02}

    saved = (dd_probe.cboe_quote, lsn.time.sleep, lsn._fetch_rh_quotes)
    dd_probe.cboe_quote = fake_quote
    lsn.time.sleep = lambda s: None
    lsn._fetch_rh_quotes = lambda syms: dict(rh_batch or {})
    lsn._QUOTES.clear()
    return saved


def _unpatch(saved):
    import dd_probe
    dd_probe.cboe_quote, lsn.time.sleep, lsn._fetch_rh_quotes = saved
    lsn._QUOTES.clear()


def test_quote_last_reads_raw_cboe_keys():
    """REGRESSION (2026-06-11): the quote dict has current_price, NOT 'last' — reading 'last'
    made every breach invisible and the sentinel never fired."""
    check("current_price read", lsn._quote_last({"current_price": 26.92}) == 26.92)
    check("legacy 'last' still accepted", lsn._quote_last({"last": 3.0}) == 3.0)
    check("empty quote -> None", lsn._quote_last({}) is None)
    check("non-dict -> None", lsn._quote_last(None) is None)
    check("string price coerced", lsn._quote_last({"current_price": "12.5"}) == 12.5)


def test_breach_fires_on_raw_cboe_quote():
    saved = _patch_quotes([11.50])  # below the 12.39 stop, both reads
    try:
        lot = {"qty": 1.0, "stop_price": 12.39, "take_profit_price": 19.0}
        hit = lsn._breach("ALOY", lot, now_s=0.0)
        check("synthetic stop fires on current_price quote",
              hit is not None and hit[0] == "synthetic_stop", hit)
    finally:
        _unpatch(saved)


def test_tier_breach_disco_overlay():
    os.environ["DISCO_SCALE_OUT_TIERS"] = "10:0.75"
    os.environ["DISCO_EXITS_LIVE"] = "1"
    os.environ["SCALE_OUT_TIERS"] = ""
    saved = _patch_quotes([112.0])  # +12% over entry 100, both reads
    try:
        lot = {"qty": 4.0, "entry_price": 100.0, "book": "disco"}
        hit = lsn._tier_breach("VELO", lot, now_s=0.0)
        check("tier fires at +12 on a 10% tier", hit is not None, hit)
        reason, last, qty_out, gains, quote = hit
        check("trim qty = 75% of init", qty_out == 3.0, qty_out)
        check("gains carry the tier", gains == [10.0], gains)
        check("reason mirrors the screen format", reason.startswith("scale-out 75% at +12"), reason)
        check("quote dict for execute_sell", quote.get("last") == 112.0 and quote.get("bid") == 111.98, quote)
        # already-taken tier never re-fires
        check("scaled tier silent", lsn._tier_breach("VELO", {**lot, "scaled": [10.0]}, 0.0) is None)
        # pead lot ignores the disco ladder (global ladder empty)
        check("pead lot has no ladder", lsn._tier_breach("VELO", {**lot, "book": "pead"}, 0.0) is None)
        # below the tier -> silent
        saved2 = _patch_quotes([105.0])
        try:
            check("below tier silent", lsn._tier_breach("VELO", dict(lot), 0.0) is None)
        finally:
            _unpatch(saved2)
        # exit already pending -> silent
        check("exit_pending suppresses", lsn._tier_breach("VELO", {**lot, "exit_pending_ts": 1.0}, 2.0) is None)
    finally:
        _unpatch(saved)
        os.environ.pop("DISCO_SCALE_OUT_TIERS", None)
        os.environ.pop("DISCO_EXITS_LIVE", None)
        os.environ.pop("SCALE_OUT_TIERS", None)


def test_realtime_rh_quotes_preferred():
    """The pass-scoped quote layer prefers the batched REAL-TIME rh_direct marks; per-symbol
    delayed Cboe is only the fallback. (Cboe CDN is ~15-min delayed — detection on it would make
    the sentinel's 1-min latency fictional.)"""
    os.environ["DISCO_SCALE_OUT_TIERS"] = "10:0.75"
    os.environ["DISCO_EXITS_LIVE"] = "1"
    os.environ["SCALE_OUT_TIERS"] = ""
    # Cboe (fallback) says +5 (no tier); the RH batch says +12 (tier due) -> RH must win
    saved = _patch_quotes([105.0], rh_batch={"VELO": {"last": 112.0, "bid": 111.9, "ask": 112.1}})
    try:
        lsn._QUOTES.update({s: q for s, q in lsn._fetch_rh_quotes(["VELO"]).items()
                            if q.get("last") is not None})  # mirror main()'s prefetch
        lot = {"qty": 4.0, "entry_price": 100.0, "book": "disco"}
        hit = lsn._tier_breach("VELO", lot, now_s=0.0)
        check("tier fires on the real-time mark", hit is not None and hit[2] == 3.0, hit)
        # a price-less batch entry must NOT mask the Cboe fallback
        lsn._QUOTES.clear()
        lsn._fetch_rh_quotes = lambda syms: {"VELO": {"last": None, "bid": None, "ask": None}}
        check("price-less batch falls back to Cboe",
              lsn._quote_last(lsn._quote("VELO")) == 105.0)
    finally:
        _unpatch(saved)
        os.environ.pop("DISCO_SCALE_OUT_TIERS", None)
        os.environ.pop("DISCO_EXITS_LIVE", None)
        os.environ.pop("SCALE_OUT_TIERS", None)


def test_tier_breach_gate_off():
    """DISCO_EXITS_LIVE=0 -> the disco ladder is ignored (falls back to the empty global)."""
    os.environ["DISCO_SCALE_OUT_TIERS"] = "10:0.75"
    os.environ["DISCO_EXITS_LIVE"] = "0"
    os.environ["SCALE_OUT_TIERS"] = ""
    saved = _patch_quotes([112.0])
    try:
        lot = {"qty": 4.0, "entry_price": 100.0, "book": "disco"}
        check("gated -> no trim", lsn._tier_breach("VELO", lot, 0.0) is None)
    finally:
        _unpatch(saved)
        os.environ.pop("DISCO_SCALE_OUT_TIERS", None)
        os.environ.pop("DISCO_EXITS_LIVE", None)
        os.environ.pop("SCALE_OUT_TIERS", None)


if __name__ == "__main__":
    tests = [test_quote_last_reads_raw_cboe_keys, test_breach_fires_on_raw_cboe_quote,
             test_tier_breach_disco_overlay, test_realtime_rh_quotes_preferred,
             test_tier_breach_gate_off]
    for fn in tests:
        fn()
    print(f"OK — {_passed} assertions passed across {len(tests)} tests")
