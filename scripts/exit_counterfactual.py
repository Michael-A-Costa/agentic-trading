#!/usr/bin/env python3
"""
exit_counterfactual.py — let-run replay on ACTUAL fills: the validation arm that replaced the
paper gate when DISCO_EXITS_LIVE flipped to 1 (2026-06-11, exit-strategy-findings §5).

For every closed round-trip in the trade history, replay the LET-RUN exit schedule (the
pre-flip live policy: stop/softcut/breakeven + trail + far TP from .env) on the same symbol,
entry date and entry FILL price using keyless daily history, and compare with what the active
policy actually realized. Because both arms share the identical entries, this measures the
exit policy alone — no venue/cadence/sizing confounds (the weakness of a paper-vs-live
comparison).

Honesty notes:
- ACTUAL returns come from real fills as recorded (real friction already in the prices); the
  counterfactual uses theoretical daily-bar prices minus --cost-bps per leg.
- The replay is daily-bar: entry-day action after the fill is not replayed; exits fire from
  day+1 on highs/lows (same engine as the backtests, backtest_quickwin.simulate).
- A trade whose counterfactual is still running (younger than --hold trading days and not yet
  stopped/TP'd in replay) is marked PARTIAL and excluded from the aggregate.

Usage:
  python3 scripts/exit_counterfactual.py                      # live trades, all books
  python3 scripts/exit_counterfactual.py --book disco --since 2026-06-11
  python3 scripts/exit_counterfactual.py --mode paper --hold 21
  python3 scripts/exit_counterfactual.py --remnant            # remnant-trail variants on the 1-min tape

Remnant mode (--remnant, A12): for every live disco scale-out harvest, replay the REMNANT's
ride on the 1-min sentinel quote tape (data/quotes-intraday.jsonl) under each pre-registered
trail variant, and compare to what the live 3% trail actually did. The variant grid is
PRE-REGISTERED (exit-strategy-findings §A12) — do not add variants after looking at results:
  flat2 / flat3 (live) / flat5 / flat8       fixed trail widths
  vscale k=1.0 / 1.25 / 1.5                  width = clamp(k x iv30_entry/sqrt252, 3, 8)
  delay3@11ET                                3% trail, armed only from 11:00 ET (breakeven before)
All variants share the breakeven floor (entry) and the TP40 cap. Decision rule (pre-registered):
at >=30 scored disco round-trips, adopt the variant that beats flat3 on mean remnant return;
ties or insufficient tape coverage keep flat3.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_gap_drift as gd
import backtest_quickwin as qw
import trade_ledger as tl

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "data" / "trades.jsonl"
QUOTE_TAPE = REPO / "data" / "quotes-intraday.jsonl"
LEDGER_TRUTH = REPO / "data" / "ledger_truth.json"  # reconcile_ledger.py --write (broker-fill truth)

# PRE-REGISTERED remnant-trail variant grid (A12) — frozen before any tape existed. Each entry:
# (label, kind, param). vscale width = clamp(k x iv30/sqrt252 daily-sigma %, 3, 8); falls back to
# rvol20 when entry iv30 is missing, and is skipped (n/a) when both are. delay arms the 3% trail
# at 11:00 ET on the harvest day (immediately if harvested later); breakeven floor active throughout.
REMNANT_VARIANTS = [
    ("flat2", "flat", 2.0), ("flat3", "flat", 3.0), ("flat5", "flat", 5.0), ("flat8", "flat", 8.0),
    ("vscale1.0", "vscale", 1.0), ("vscale1.25", "vscale", 1.25), ("vscale1.5", "vscale", 1.5),
    ("delay3@11ET", "delay", 3.0),
]


def envf(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "") or default)
    except ValueError:
        return default


def letrun_policy(cost_bps: float) -> dict:
    """The pre-flip live exit schedule, read from .env so the counterfactual tracks the
    actual let-run config (stop12/sc8/be12 + trail15@20 + tp40 at today's values)."""
    pol = {"stop": envf("STOP_LOSS_PCT", 12.0), "softcut": envf("SOFT_CUT_PCT", 8.0),
           "be": envf("TRAIL_BREAKEVEN_AT_PCT", 12.0), "tp": envf("TAKE_PROFIT_PCT", 40.0),
           "cost_bps": cost_bps}
    trail, act = envf("TRAIL_STOP_PCT", 15.0), envf("TRAIL_ACTIVATE_PCT", 20.0)
    if trail > 0:
        pol["trail"], pol["activate"] = trail, act
    return pol


def load_broker_exit_prices() -> dict[str, list[tuple[datetime, float]]]:
    """Per-symbol broker-fill exit prices from data/ledger_truth.json (reconcile_ledger.py --write):
    {SYM: [(exit_dt_utc, exit_price), ...]} sorted by time. This is the SETTLEMENT-TRUTH price used to
    backfill the trades.jsonl sell rows that logged `price: null` (live MARKET exits — see
    [[live-realized-broker-truth]]); without it those round-trips never pair and silently vanish from
    the let-run scorer (the BRUN-shaped blind spot). Deduped by exit_order_id since one broker order can
    span several FIFO chunks that all share its average_price."""
    out: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    if not LEDGER_TRUTH.exists():
        return out
    try:
        d = json.loads(LEDGER_TRUTH.read_text())
    except (OSError, ValueError):
        return out
    seen: set = set()
    for t in d.get("round_trips", []):
        sym = str(t.get("symbol", "")).upper()
        px, ts, oid = t.get("exit_price"), t.get("exit_ts"), t.get("exit_order_id")
        if px is None or not ts:
            continue
        key = (sym, oid, ts)
        if oid and key in seen:
            continue
        seen.add(key)
        try:  # reconcile_ledger stores exit_ts as "YYYY-MM-DD HH:MM:SS" (UTC)
            dt = datetime.fromisoformat(ts.replace(" ", "T")).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        out[sym].append((dt, float(px)))
    for v in out.values():
        v.sort()
    return out


def backfill_exit_price(sym: str, ts_utc: str, broker_exits: dict, window_min: float = 30.0):
    """The broker-fill exit price for sym closest in time to a null-logged sell, within +/- window_min.
    None when no broker exit matches (then the round-trip stays dropped, as before)."""
    cands = broker_exits.get(sym, [])
    if not cands:
        return None
    try:
        t = datetime.fromisoformat((ts_utc or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    best, best_gap = None, window_min * 60
    for dt, px in cands:
        gap = abs((dt - t).total_seconds())
        if gap <= best_gap:
            best, best_gap = px, gap
    return best


def round_trips_with_meta(rows: list[dict], broker_exits: dict | None = None) -> list[dict]:
    """FIFO entry/exit pairing like trade_ledger.build_round_trips, but carrying the buy row's
    entry date and book (needed to align the replay and split the aggregate). A live MARKET-exit sell
    logs `price: null`; when broker_exits is supplied we backfill that price from settlement truth so
    the round-trip pairs instead of vanishing — each trip carries price_src ('log' | 'broker_backfill')."""
    open_lots: dict[str, deque] = defaultdict(deque)  # sym -> deque of [qty, price, date, book]
    trips: list[dict] = []
    for r in rows:
        sym = str(r.get("symbol", "")).upper()
        side = str(r.get("side", "")).lower()
        qty = float(r.get("qty") or 0.0)
        price = r.get("price")
        price_src = "log"
        if side == "sell" and price is None and broker_exits is not None:
            price = backfill_exit_price(sym, r.get("ts_utc") or "", broker_exits)
            if price is not None:
                price_src = "broker_backfill"
        date = (r.get("ts_et") or r.get("ts_utc") or "")[:10]
        if qty <= 0 or price is None or not date:
            continue
        if side == "buy":
            open_lots[sym].append([qty, float(price), date, r.get("book") or "untagged"])
            continue
        remaining = qty
        lots = open_lots[sym]
        while remaining > 1e-9 and lots:
            lot = lots[0]
            take = min(remaining, lot[0])
            trips.append({"symbol": sym, "qty": take, "entry_price": lot[1],
                          "entry_date": lot[2], "book": lot[3],
                          "exit_price": float(price), "exit_date": date,
                          "price_src": price_src,
                          "exit_type": r.get("exit_type", "other"),
                          # manage-DD A/B arm that produced this exit (None for stop/EOD/TP exits)
                          "manage_arm": r.get("manage_arm"),
                          "manage_model": r.get("manage_model")})
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-9:
                lots.popleft()
    return trips


def bars_for(sym: str, exit_date: str, cache: dict, refresh: bool) -> list[dict]:
    """Cleaned daily bars, auto-refetching when the cached history doesn't reach past the
    exit (the counterfactual needs post-exit bars to keep running the let-run schedule)."""
    if sym not in cache:
        bars = gd.clean_bars(gd.load_bars(sym, refresh))
        if (not bars or bars[-1]["date"] <= exit_date) and not refresh:
            bars = gd.clean_bars(gd.load_bars(sym, True))
        cache[sym] = bars
    return cache[sym]


def load_tape() -> dict[str, list[tuple[str, str, float]]]:
    """1-min sentinel tape -> per-symbol [(ts_utc, ts_et, last), ...] in time order."""
    import json
    paths: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    if not QUOTE_TAPE.exists():
        return paths
    for line in QUOTE_TAPE.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        ts_utc, ts_et = row.get("ts_utc") or "", row.get("ts_et") or ""
        for sym, last in (row.get("quotes") or {}).items():
            if isinstance(last, (int, float)) and last > 0:
                paths[sym.upper()].append((ts_utc, ts_et, float(last)))
    for p in paths.values():
        p.sort()
    return paths


def trail_width(kind: str, param: float, iv30: float | None, rvol20: float | None) -> float | None:
    """Resolve a variant's trail width in % for one lot; None = variant not scorable (no vol)."""
    if kind in ("flat", "delay"):
        return param
    vol = iv30 if iv30 is not None else rvol20
    if vol is None:
        return None
    return min(8.0, max(3.0, param * vol / (252 ** 0.5)))


def replay_remnant(path: list[tuple[str, str, float]], entry: float, width: float,
                   tp_px: float, delay_arm: bool) -> tuple[str, float, str]:
    """Walk the 1-min path from the harvest forward under one trail rule.
    Returns (status, exit_or_last_price, ts) where status is 'trail' | 'floor' | 'tp' | 'riding'.
    Floor = breakeven (entry; SCALE_BREAKEVEN_AFTER_FIRST). delay_arm holds the trail dormant
    until 11:00 ET on the harvest day (high-water still tracks from the first post-harvest mark)."""
    hw = path[0][2]
    arm_after = path[0][1][:10] + "T11:00:00" if delay_arm else ""
    for ts_utc, ts_et, last in path:
        hw = max(hw, last)
        if last >= tp_px:
            return "tp", tp_px, ts_utc
        if last <= entry:
            return "floor", entry, ts_utc
        if (not delay_arm or ts_et >= arm_after) and last <= hw * (1 - width / 100.0):
            return "trail", last, ts_utc
    return "riding", path[-1][2], path[-1][0]


def replay_wholelot(path: list[tuple[str, str, float]], floor_px: float, width: float,
                    tp_px: float, delay_arm: bool) -> tuple[str, float, str]:
    """Walk the 1-min path from the +8% ARM forward under one WHOLE-LOT trail rule (A17).
    Returns (status, exit_or_last_price, ts), status in 'trail' | 'floor' | 'tp' | 'riding'.
    Identical mechanics to replay_remnant except the floor is the be5+1% rung (entry x 1.01),
    not breakeven — the live net for a full lot. High-water tracks from the arm mark."""
    hw = path[0][2]
    arm_after = path[0][1][:10] + "T11:00:00" if delay_arm else ""
    for ts_utc, ts_et, last in path:
        hw = max(hw, last)
        if last >= tp_px:
            return "tp", tp_px, ts_utc
        if last <= floor_px:
            return "floor", floor_px, ts_utc
        if (not delay_arm or ts_et >= arm_after) and last <= hw * (1 - width / 100.0):
            return "trail", last, ts_utc
    return "riding", path[-1][2], path[-1][0]


def wholelot_report(cost_bps: float) -> int:
    """--wholelot (A17): score the SAME pre-registered grid as --remnant on every live lot that
    armed the whole-lot trail (+8%) and was never harvested (no scale_out) — i.e. the full lot
    rode the trail. Replays each variant from the arm point on the 1-min sentinel tape, floored by
    be5+1%, capped by TP. Grid + decision rule frozen in §A17 — do NOT add variants post-hoc."""
    import json
    from datetime import datetime
    rows = tl.load_rows(LEDGER, None, None, "live", None)
    tape = load_tape()
    tp_cap = envf("TAKE_PROFIT_PCT", 40.0)
    be_off = envf("TRAIL_BREAKEVEN_OFFSET_PCT", 1.0)
    act_base = envf("TRAIL_ACTIVATE_PCT", 8.0)
    act_disco = envf("DISCO_TRAIL_ACTIVATE_PCT", 8.0)
    cost = cost_bps / 10000.0 * 2  # entry + exit leg, uniform across variants (cancels in deltas)
    try:
        backfill = json.loads((REPO / "data" / "entry_vol_backfill.json").read_text())
    except (OSError, ValueError):
        backfill = {}
    # FIFO lots; flag any lot a scale_out harvest touched (those are §A12 remnant cases, not here)
    # and record the lot's realized exit(s) for the reference column.
    by_sym: dict[str, deque] = defaultdict(deque)
    all_lots: list[dict] = []
    for r in rows:
        sym = str(r.get("symbol", "")).upper()
        side = str(r.get("side", "")).lower()
        qty = float(r.get("qty") or 0.0)
        if side == "buy" and r.get("price") is not None and qty > 0:
            lot = {"sym": sym, "buy": r, "qty_left": qty, "harvested": False, "exits": []}
            by_sym[sym].append(lot)
            all_lots.append(lot)
            continue
        if side != "sell" or qty <= 0 or r.get("price") is None:
            continue
        et = r.get("exit_type")
        remaining, dq = qty, by_sym[sym]
        while remaining > 1e-9 and dq:
            lot = dq[0]
            take = min(remaining, lot["qty_left"])
            lot["exits"].append((take, float(r["price"]), r.get("ts_utc") or "", et))
            if et == "scale_out":
                lot["harvested"] = True
            lot["qty_left"] -= take
            remaining -= take
            if lot["qty_left"] <= 1e-9:
                dq.popleft()
    cands = [l for l in all_lots if not l["harvested"] and l["exits"]]  # closed, never harvested
    if not cands:
        print("no closed whole-lot (non-harvested) live lots in the ledger yet")
        return 0
    if not tape:
        print(f"{len(cands)} whole-lot candidate(s) but no quote tape yet ({QUOTE_TAPE.name})")
        return 0

    print(f"WHOLE-LOT trail replay — {len(cands)} live non-harvested lot(s), pre-registered grid "
          f"(A17 = A12 grid), be{envf('TRAIL_BREAKEVEN_AT_PCT', 5):.0f}+{be_off:.0f}% floor + "
          f"TP{tp_cap:.0f} cap, {cost_bps:.0f}bps/leg\n")
    agg: dict[str, list[float]] = defaultdict(list)
    for lot in cands:
        b = lot["buy"]
        sym = lot["sym"]
        entry = float(b["price"])
        ets = b.get("ts_utc") or ""
        book = str(b.get("book") or "untagged")
        act = act_disco if book == "disco" else act_base
        arm_px = entry * (1 + act / 100.0)
        floor_px = entry * (1 + be_off / 100.0)
        tp_px = entry * (1 + tp_cap / 100.0)
        date = (b.get("ts_et") or ets or "")[:10]
        bf = backfill.get(f"{sym}:{date}", {})
        iv30 = b.get("iv30") if b.get("iv30") is not None else bf.get("iv30")
        rvol20 = b.get("rvol20") if b.get("rvol20") is not None else bf.get("rvol20")
        vol_note = (f"iv30={iv30:.0f}%" if iv30 is not None else
                    (f"rvol20={rvol20:.0f}%" if rvol20 is not None else "no entry vol"))
        # qty-weighted realized of the closed portion (the live outcome, for reference)
        qsum = sum(q for q, _, _, _ in lot["exits"])
        realized = (sum(q * px for q, px, _, _ in lot["exits"]) / qsum) / entry - 1.0 if qsum else 0.0
        last_exit_ts = max((ts for _, _, ts, _ in lot["exits"] if ts), default="")
        path = [p for p in tape.get(sym, []) if p[0] >= ets]
        if not path:
            print(f"{sym:<6} entry {entry:.2f} {date} ({vol_note}) realized {realized*100:+.2f}% "
                  f"— no tape coverage (excluded)")
            continue
        if path[0][2] >= arm_px:  # armed before the tape started → run-up/high-water unobserved
            print(f"{sym:<6} entry {entry:.2f} {date} ({vol_note}) realized {realized*100:+.2f}% "
                  f"— TAPE-GAP: armed (+{act:.0f}%) before tape coverage, EXCLUDED")
            continue
        arm_i = next((k for k, p in enumerate(path) if p[2] >= arm_px), None)
        if arm_i is None:
            print(f"{sym:<6} entry {entry:.2f} {date} ({vol_note}) realized {realized*100:+.2f}% "
                  f"— never reached +{act:.0f}% on tape (floor case, not a trail case)")
            continue
        arm_path = path[arm_i:]
        # tape truncates when the live position closes → wider variants can't be seen past our exit
        trunc = ""
        if last_exit_ts and path[-1][0] <= last_exit_ts:
            trunc = "  [TAPE ENDS AT LIVE EXIT — wider-variant returns are LOWER BOUNDS]"
        elif last_exit_ts:
            tail = (datetime.fromisoformat(path[-1][0]) -
                    datetime.fromisoformat(last_exit_ts)).total_seconds() / 60
            if tail < 10:
                trunc = f"  [tape ends {tail:.0f}m after exit — wider variants partly unobserved]"
        peak = max(p[2] for p in arm_path)
        print(f"{sym:<6} entry {entry:.2f} {date}  armed +{act:.0f}% @ {arm_px:.2f}  "
              f"peak {peak:.2f} (+{(peak/entry-1)*100:.1f}%)  realized {realized*100:+.2f}%  "
              f"({vol_note}, {len(arm_path)} marks){trunc}")
        for label, kind, param in REMNANT_VARIANTS:
            w = trail_width(kind, param, iv30, rvol20)
            if w is None:
                print(f"    {label:<12} n/a (no entry vol recorded)")
                continue
            status, px, ts = replay_wholelot(arm_path, floor_px, w, tp_px, kind == "delay")
            ret = px / entry - 1.0 - cost
            agg[label].append(ret)
            print(f"    {label:<12} w={w:.1f}%  {status:<7} @ {px:.2f}  ({ret*100:+.2f}%)"
                  + (f"  {ts[:16]}" if status != "riding" else "  (riding @ tape end)"))
    if agg:
        n = max(len(v) for v in agg.values())
        print(f"\nper-variant mean whole-lot return (n scored, {n} max):")
        base = sum(agg.get("flat3", [])) / len(agg["flat3"]) if agg.get("flat3") else None
        for label, _, _ in REMNANT_VARIANTS:
            rets = agg.get(label)
            if rets:
                m = sum(rets) / len(rets)
                delta = f"  ({(m-base)*100:+.2f}% vs flat3)" if base is not None and label != "flat3" else ""
                print(f"  {label:<12} {m*100:+.2f}%  (n={len(rets)}){delta}")
        print(f"\ndecision rule (pre-registered, A17): at >=30 scored whole-lot round-trips adopt "
              f"the variant beating flat3 on mean realized return; ties / insufficient tape keep "
              f"flat3. CURRENTLY n={n} — {'BELOW' if n < 30 else 'AT/ABOVE'} the bar, no dial change.")
    return 0


def remnant_report() -> int:
    """--remnant: score the pre-registered trail variants on every live disco harvest with tape."""
    import json
    rows = tl.load_rows(LEDGER, None, None, "live", None)
    tape = load_tape()
    tp_cap = envf("TAKE_PROFIT_PCT", 40.0)
    # Entry-time vol for pre-A12 buy rows (sidecar — the append-only ledger is never edited).
    try:
        backfill = json.loads((REPO / "data" / "entry_vol_backfill.json").read_text())
    except (OSError, ValueError):
        backfill = {}
    # FIFO entries per symbol (qty-consuming, same pairing as the ledger) so each scale-out
    # harvest ties back to ITS entry row (price + entry-time vol), not a stale earlier lot.
    entries: dict[str, deque] = defaultdict(deque)  # sym -> deque of [qty_left, buy_row]
    harvests = []  # (sym, harvest_ts_utc, entry_price, iv30, rvol20)
    for r in rows:
        sym = str(r.get("symbol", "")).upper()
        side = str(r.get("side", "")).lower()
        qty = float(r.get("qty") or 0.0)
        if side == "buy" and r.get("price") is not None and qty > 0:
            entries[sym].append([qty, r])
            continue
        if side != "sell" or qty <= 0:
            continue
        lots = entries[sym]
        if r.get("exit_type") == "scale_out" and lots:
            e = lots[0][1]  # the harvested lot = oldest open entry (FIFO)
            if "disco" in (str(e.get("book") or ""), str(r.get("book") or "")):
                bf = backfill.get(f"{sym}:{((e.get('ts_et') or e.get('ts_utc') or '')[:10])}", {})
                harvests.append((sym, r.get("ts_utc") or "", float(e["price"]),
                                 e.get("iv30") if e.get("iv30") is not None else bf.get("iv30"),
                                 e.get("rvol20") if e.get("rvol20") is not None else bf.get("rvol20")))
        remaining = qty
        while remaining > 1e-9 and lots:
            take = min(remaining, lots[0][0])
            lots[0][0] -= take
            remaining -= take
            if lots[0][0] <= 1e-9:
                lots.popleft()
    if not harvests:
        print("no live disco scale-out harvests in the ledger yet")
        return 0
    if not tape:
        print(f"{len(harvests)} harvest(s) found but no quote tape yet ({QUOTE_TAPE.name} — "
              f"the 1-min sentinel starts recording it on its next open-market pass)")
        return 0

    print(f"REMNANT replay — {len(harvests)} live disco harvest(s), pre-registered grid (A12), "
          f"breakeven floor + TP{tp_cap:.0f} cap on all variants\n")
    agg: dict[str, list[float]] = defaultdict(list)
    for sym, hts, entry, iv30, rvol20 in harvests:
        path = [p for p in tape.get(sym, []) if p[0] >= hts]
        vol_note = f"iv30={iv30:.0f}%" if iv30 is not None else (
            f"rvol20={rvol20:.0f}%" if rvol20 is not None else "no entry vol")
        if not path:
            print(f"{sym:<6} harvested {hts[:16]}  entry {entry:.2f}  ({vol_note}) — no tape coverage")
            continue
        # Tape that starts well after the harvest missed the post-trim whipsaw window (the very
        # thing being measured) — flag it so a partially-observed remnant isn't read as clean.
        lag_min = (datetime.fromisoformat(path[0][0]) -
                   datetime.fromisoformat(hts)).total_seconds() / 60 if hts else 0.0
        gap = f", TAPE GAP: starts {lag_min:.0f}m after harvest" if lag_min > 5 else ""
        print(f"{sym:<6} harvested {hts[:16]}  entry {entry:.2f}  ({vol_note}, "
              f"{len(path)} tape marks{gap})")
        tp_px = entry * (1 + tp_cap / 100.0)
        for label, kind, param in REMNANT_VARIANTS:
            w = trail_width(kind, param, iv30, rvol20)
            if w is None:
                print(f"    {label:<12} n/a (no entry vol recorded)")
                continue
            status, px, ts = replay_remnant(path, entry, w, tp_px, kind == "delay")
            ret = px / entry - 1.0
            agg[label].append(ret)
            print(f"    {label:<12} w={w:.1f}%  {status:<7} @ {px:.2f}  ({ret * 100:+.2f}%)"
                  + (f"  {ts[:16]}" if status != "riding" else "  (still riding)"))
    if agg:
        print("\nper-variant mean remnant return (n scored):")
        for label, _, _ in REMNANT_VARIANTS:
            rets = agg.get(label)
            if rets:
                print(f"  {label:<12} {sum(rets) / len(rets) * 100:+.2f}%  (n={len(rets)})")
        print("\ndecision rule (pre-registered, A12): adopt the variant beating flat3 on mean "
              "remnant return at >=30 scored disco round-trips; otherwise keep flat3.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="live", help="live | paper | all (default live)")
    ap.add_argument("--book", default=None, help="pead | disco | untagged (default all)")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD floor on trade timestamps")
    ap.add_argument("--hold", type=int, default=int(envf("MAX_HOLD_DAYS", 21)),
                    help="counterfactual horizon in trading days (default MAX_HOLD_DAYS)")
    ap.add_argument("--cost-bps", type=float, default=15.0)
    ap.add_argument("--refresh", action="store_true", help="refetch all price history")
    ap.add_argument("--remnant", action="store_true",
                    help="replay remnant-trail variants on the 1-min sentinel quote tape (A12)")
    ap.add_argument("--wholelot", action="store_true",
                    help="replay whole-lot trail variants on every non-harvested armed lot (A17)")
    args = ap.parse_args()

    if args.remnant:
        return remnant_report()
    if args.wholelot:
        return wholelot_report(args.cost_bps)

    rows = tl.load_rows(LEDGER, args.since, None, args.mode, args.book)
    # Backfill null-priced live MARKET exits from broker settlement truth so they pair (see
    # load_broker_exit_prices). Skip for paper (paper fills always carry a price).
    broker_exits = load_broker_exit_prices() if args.mode in ("live", "all") else None
    if args.mode in ("live", "all") and not broker_exits:
        print(f"note: {LEDGER_TRUTH.name} missing/empty — null-priced live exits will be DROPPED. "
              f"Run `python3 scripts/reconcile_ledger.py --write` to backfill them.\n")
    trips = round_trips_with_meta(rows, broker_exits)
    if not trips:
        print("no closed round-trips match the filters")
        return 0
    n_backfilled = sum(1 for t in trips if t.get("price_src") == "broker_backfill")

    pol = letrun_policy(args.cost_bps)
    pol_desc = (f"stop{pol['stop']:.0f}/sc{pol['softcut']:.0f}/be{pol['be']:.0f}"
                f"/tp{pol['tp']:.0f}" + (f"/tr{pol['trail']:.0f}@{pol['activate']:.0f}"
                                         if "trail" in pol else ""))
    print(f"LET-RUN counterfactual ({pol_desc}, hold {args.hold}d, {args.cost_bps:.0f}bps/leg) "
          f"vs ACTUAL exits — {len(trips)} closed round-trip(s)\n")
    hdr = (f"{'exit date':<11}{'sym':<6}{'book':<9}{'entry':>8}{'exit':>8}{'actual%':>8} "
           f"{'exit_type':<12}{'cf%':>7}{'cf_day':>7}  {'delta':>7}")
    print(hdr)
    print("-" * len(hdr))

    cache: dict = {}
    complete, partial, unpriced = [], [], []
    for t in trips:
        sym = t["symbol"]
        bf = " bf" if t.get("price_src") == "broker_backfill" else ""  # exit price from broker truth
        actual = t["exit_price"] / t["entry_price"] - 1.0
        try:
            bars = bars_for(sym, t["exit_date"], cache, args.refresh)
        except Exception:
            bars = []
        i = next((k for k, b in enumerate(bars) if b["date"] >= t["entry_date"]), None)
        if i is None or not bars or bars[i]["date"] != t["entry_date"]:
            unpriced.append(t)
            print(f"{t['exit_date']:<11}{sym:<6}{t['book']:<9}{t['entry_price']:>8.2f}"
                  f"{t['exit_price']:>8.2f}{actual * 100:>+7.2f}% {t['exit_type']:<12}"
                  f"{'no bars':>7}{bf}")
            continue
        horizon = min(args.hold, len(bars) - 1 - i)
        if horizon < 1:
            unpriced.append(t)
            continue
        cf, cf_day = qw.simulate(bars, i, t["entry_price"], horizon, pol)[:2]
        is_partial = horizon < args.hold and cf_day == horizon  # still running in replay
        rec = dict(t, actual=actual, cf=cf, cf_day=cf_day, delta=actual - cf)
        (partial if is_partial else complete).append(rec)
        print(f"{t['exit_date']:<11}{sym:<6}{t['book']:<9}{t['entry_price']:>8.2f}"
              f"{t['exit_price']:>8.2f}{actual * 100:>+7.2f}% {t['exit_type']:<12}"
              f"{cf * 100:>+6.2f}%{cf_day:>6}d  {(actual - cf) * 100:>+6.2f}%"
              + ("  PARTIAL" if is_partial else "") + bf)

    def agg(label: str, recs: list[dict]) -> None:
        if not recs:
            return
        a = [r["actual"] for r in recs]
        c = [r["cf"] for r in recs]
        d = [r["delta"] for r in recs]
        n = len(recs)
        print(f"\n{label} (n={n}):")
        print(f"  actual : mean {sum(a) / n * 100:+.2f}%  median {gd.median(a) * 100:+.2f}%  "
              f"win {sum(1 for x in a if x > 0) / n * 100:.0f}%")
        print(f"  let-run: mean {sum(c) / n * 100:+.2f}%  median {gd.median(c) * 100:+.2f}%  "
              f"win {sum(1 for x in c if x > 0) / n * 100:.0f}%")
        print(f"  delta  : mean {sum(d) / n * 100:+.2f}%/trade  "
              f"(positive = active policy beat let-run on these entries)")

    agg("ALL COMPLETE", complete)
    for book in sorted({r["book"] for r in complete}):
        agg(f"book={book}", [r for r in complete if r["book"] == book])
    # manage-DD A/B split: compare realized exits of round-trips a manage model trimmed/exited.
    # Only round-trips whose exit was manage-driven carry an arm; stop/EOD/TP exits are untagged.
    manage_trips = [r for r in complete if r.get("manage_arm")]
    if manage_trips:
        print("\n--- manage-DD A/B (manage-driven exits only) ---")
        for arm in sorted({r["manage_arm"] for r in manage_trips}):
            sub = [r for r in manage_trips if r["manage_arm"] == arm]
            model = next((r.get("manage_model") for r in sub if r.get("manage_model")), "?")
            agg(f"arm {arm} [{model}]", sub)
    if partial:
        print(f"\n{len(partial)} PARTIAL row(s) excluded from aggregates "
              f"(counterfactual still running — re-run after more sessions)")
    if unpriced:
        print(f"{len(unpriced)} round-trip(s) had no usable daily history (see 'no bars' rows)")
    if n_backfilled:
        print(f"{n_backfilled} exit(s) priced from broker settlement truth ('bf' rows — null in "
              f"trades.jsonl, recovered via {LEDGER_TRUTH.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
