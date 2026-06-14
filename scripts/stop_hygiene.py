#!/usr/bin/env python3
"""
stop_hygiene.py — stop-price QUANTIZATION hygiene: a pure helper + a replay diagnostic.

Origin: 2026-06-13/14 fintwit review (L2WTrades / thedelost "your stop gets hit because it's
the most predictable price"). Our resting stops are %-off-entry, NOT chart-derived, so we're
already immune to the worst form (stops parked at a visible swing low / MA). Two small residual
exposures remain, both at the cent-quantization step that live_execute does via round()/f"{:.2f}":

  1. round() can NUDGE A SELL-STOP UP (toward price) by up to ~0.5c → slightly tighter risk than
     intended. For a protective stop you always want the rounding to fall AWAY from price.
  2. round() occasionally lands the stop EXACTLY on a whole-/half-dollar MAGNET ($50.00, $49.50)
     — the liquidity pool where wicks gravitate and crowd-stops cluster.

This module provides the candidate fix as a PURE function (so it can be unit-checked and, only
after replay clears it, imported by live_execute) plus a replay diagnostic that measures how
often the fix would change a real stop trigger on our actual fills + the 1-min sentinel tape.

NOTHING here is wired into live_execute. Per the standing rule (cent-precision-compounds /
exit-policy-tuning): prove cent-level changes in replay over >=30 round-trips, never by eyeballing.

Usage:
  python3 scripts/stop_hygiene.py --selfcheck          # deterministic invariants, no data needed
  python3 scripts/stop_hygiene.py --replay             # measure A(round) vs B(floor+nudge) on tape
  python3 scripts/stop_hygiene.py --replay --band 0.05 --off 0.03
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "data" / "trades.jsonl"
TAPE = REPO / "data" / "quotes-intraday.jsonl"


# ---------------------------------------------------------------------------
# PURE quantizers. A sell-stop sits BELOW price; "safer" always means LOWER
# (more room, never a tighter trigger than intended).
# ---------------------------------------------------------------------------
def q_round(price: float) -> float:
    """CURRENT live behaviour: round to the nearest cent (can tighten by <=0.5c)."""
    return round(price, 2)


def q_floor(price: float) -> float:
    """Floor to the cent: cent-quantization can only LOOSEN a sell-stop, never tighten it."""
    return math.floor(price * 100) / 100.0


def q_floor_nudge(price: float, band: float = 0.05, off: float = 0.03,
                  half_dollar: bool = True) -> float:
    """Floor, then step a few cents BELOW the nearest whole-/half-dollar magnet at or just above
    the floored level. Guaranteed never to RAISE the stop (uses min()), so risk only ever loosens.

      band — how close (in $) the floored stop must be to a magnet to bother nudging.
      off  — how far below the magnet to place the stop ($).
      half_dollar — also treat $X.50 as a (weaker) magnet, not just whole dollars.

    Examples (band=0.05, off=0.03):
      50.00 -> 49.97   (sat on the magnet)
      49.99 -> 49.97   (just above floor of magnet)
      49.96 -> 49.96   (within band but magnet-0.03=49.97 > 49.96, so min() keeps 49.96)
      49.50 -> 49.47   (half-dollar magnet)
      49.40 -> 49.40   (no magnet within band)
    """
    base = q_floor(price)
    # nearest whole-dollar at or above base
    magnets = [float(math.ceil(base - 1e-9))]  # e.g. base 49.99 -> 50 ; base 49.00 -> 49
    if half_dollar:
        hd = math.floor(base) + 0.5
        if hd >= base - 1e-9:
            magnets.append(hd)
        else:
            magnets.append(hd + 1.0)
    cand = base
    for m in magnets:
        if 0.0 <= (m - base) <= band + 1e-9:
            cand = min(cand, round(m - off, 2))
    return round(cand, 2)


# ---------------------------------------------------------------------------
# Replay diagnostic
# ---------------------------------------------------------------------------
def _load_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def closed_roundtrips(rows: list) -> list:
    """FIFO-pair buys/sells per symbol; return CLOSED whole-share lots (the magnet fix only applies
    to whole-share resting stops — fractional lots get a synthetic stop, not a broker order)."""
    from collections import defaultdict, deque
    books = defaultdict(deque)
    closed = []
    for r in rows:
        if r.get("status") != "filled":
            continue
        sym, side, qty, px = r.get("symbol"), r.get("side"), r.get("qty", 0.0), r.get("price")
        if not sym or px is None:
            continue
        if side == "buy":
            books[sym].append([qty, px])
        elif side == "sell":
            rem = qty
            while rem > 1e-9 and books[sym]:
                lot = books[sym][0]
                take = min(rem, lot[0])
                closed.append({"symbol": sym, "entry": lot[1], "exit": px, "qty": take})
                lot[0] -= take
                rem -= take
                if lot[0] <= 1e-9:
                    books[sym].popleft()
    return closed


def replay(band: float, off: float, stop_pct: float = 8.0) -> None:
    rows = _load_jsonl(LEDGER)
    tape = _load_jsonl(TAPE)
    rts = closed_roundtrips(rows)
    whole = [rt for rt in rts if abs(rt["qty"] - round(rt["qty"])) < 1e-6 and rt["qty"] >= 1]

    # per-symbol min price seen on the 1-min tape (the only place an intraday wick to a magnet shows)
    tape_low = {}
    tape_syms = set()
    for snap in tape:
        for sym, q in (snap.get("quotes") or {}).items():
            tape_syms.add(sym)
            if q is not None:
                tape_low[sym] = min(tape_low.get(sym, q), q)

    print(f"closed round-trips: {len(rts)}  (whole-share: {len(whole)})")
    print(f"1-min tape: {len(tape)} snapshots, {len(tape_syms)} symbols, "
          f"low-water for {len(tape_low)} of them")
    print(f"params: catastrophe stop_pct={stop_pct}%  magnet band=${band:.2f}  off=${off:.2f}\n")

    hdr = f"{'sym':<6}{'entry':>9}{'stopA(round)':>14}{'stopB(nudge)':>14}{'Δstop':>8}{'tapeLow':>10}  note"
    print(hdr)
    print("-" * len(hdr))
    n_band = n_trigger_diff = 0
    net_bps = 0.0
    for rt in whole:
        sym, entry = rt["symbol"], rt["entry"]
        raw = entry * (1 - stop_pct / 100.0)          # the catastrophe stop level (initial)
        a = q_round(raw)
        b = q_floor_nudge(raw, band=band, off=off)
        in_band = abs(a - b) > 1e-9
        n_band += in_band
        lo = tape_low.get(sym)
        note = ""
        if lo is not None and a != b and b < lo <= a:
            # price dipped into [B, A]: arm A would have stopped, arm B would have survived
            n_trigger_diff += 1
            net_bps += (a / entry - (lo / entry)) * 1e4  # crude: avoided loss vs trigger at A
            note = "TRIGGER DIFF (B survives A's stop)"
        elif lo is None:
            note = "no tape"
        print(f"{sym:<6}{entry:>9.2f}{a:>14.2f}{b:>14.2f}{(b-a):>8.2f}"
              f"{(lo if lo is not None else float('nan')):>10.2f}  {note}")

    print(f"\nstops in magnet band (A!=B): {n_band}/{len(whole)}")
    print(f"trigger-differences over available tape: {n_trigger_diff}")
    if n_trigger_diff:
        print(f"  crude avoided-loss on those: ~{net_bps:.0f} bps total")
    print("\nVERDICT:", _verdict(len(whole), n_trigger_diff))


def _verdict(n_whole: int, n_trig: int) -> str:
    if n_whole < 30:
        return (f"INCONCLUSIVE — only {n_whole} closed whole-share round-trips (<30 bar). "
                "Re-run after the cohort matures (~2026-06-26) and the 1-min tape accumulates "
                "multi-session coverage. Do NOT ship the fix on this sample.")
    if n_trig == 0:
        return ("NO MEASURABLE EFFECT on this sample — the magnet/round bias never changed a real "
                "trigger. Fix is harmless (strictly loosens by <=few cents) but unproven to help.")
    return (f"{n_trig} trigger-difference(s) observed — fix avoided being stopped at a magnet. "
            "Promising; verify the avoided-loss holds out-of-sample before shipping.")


def selfcheck() -> None:
    """Deterministic invariants — no market data. Fails loudly if a property is violated."""
    cases = [50.00, 49.99, 49.96, 49.50, 49.40, 49.005, 100.00, 12.34, 25.50, 7.01]
    print(f"{'raw':>9}{'round':>9}{'floor':>9}{'nudge':>9}  invariant")
    ok = True
    for x in cases:
        r, f, n = q_round(x), q_floor(x), q_floor_nudge(x)
        # 1) floor never tighter (higher) than the true level
        # 2) nudge never higher than floor (never tightens vs floor)
        inv = (f <= x + 1e-9) and (n <= f + 1e-9)
        ok = ok and inv
        print(f"{x:>9.3f}{r:>9.2f}{f:>9.2f}{n:>9.2f}  {'ok' if inv else 'FAIL'}")
    # round() tightening demonstration: a level that rounds UP
    x = 49.265
    print(f"\nround({x}) = {q_round(x):.2f} (UP, tighter)   "
          f"floor = {q_floor(x):.2f} (down, safer)")
    print("\nSELFCHECK", "PASSED" if ok else "FAILED")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--band", type=float, default=0.05, help="magnet proximity ($)")
    ap.add_argument("--off", type=float, default=0.03, help="cents below magnet ($)")
    ap.add_argument("--stop-pct", type=float, default=8.0, help="catastrophe stop %% off entry")
    args = ap.parse_args()
    if args.selfcheck:
        selfcheck()
    elif args.replay:
        replay(args.band, args.off, args.stop_pct)
    else:
        ap.print_help()
