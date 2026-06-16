#!/usr/bin/env python3
"""reconcile_ledger.py — broker order history is the ONLY ground truth for realized P&L.

Why this exists: data/trades.jsonl is an append-only EVENT log of what the engine *intended*
(placed) and best-effort caught afterward. On the live path it drifts badly from broker reality —
market exits log `price: null`, external/stop closures book under a different order_id than the
placed exit, and `realized_est_usd` (a place-time estimate) coexists with `realized_usd` (truth).
The result: trade_ledger.py's FIFO can pair only a handful of exits, leaves dozens of phantom-open
lots, and overstates realized via survivorship. pnl_report.py reads the engine-log and finds *zero*
live sells. Three tools, three different "realized" numbers, none defensible.

This sidesteps all of that: it pulls the broker's own filled-order record (get_equity_orders,
placed_agent="agentic") — id, side, quantity, `average_price`, executions[] — and FIFO-pairs THOSE.
Broker fills are the settlement record; there is nothing more authoritative. The reconstructed open
book is asserted against the live broker positions, so a mismatch is a loud error, not silent drift.

Outputs:
  • a human report (realized P&L, round-trips, per-symbol, exit-type, open book vs broker)
  • data/ledger_truth.json  — machine-readable truth the dashboard + pnl_report consume (--write)

Usage:
    python3 scripts/reconcile_ledger.py                 # report to stdout
    python3 scripts/reconcile_ledger.py --write         # also write data/ledger_truth.json
    python3 scripts/reconcile_ledger.py --json          # raw JSON to stdout (no prose)
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rh_direct
from trade_log import classify_exit, EXIT_LABEL

REPO = Path(__file__).resolve().parent.parent
TRADES_LOG = REPO / "data" / "trades.jsonl"
QUOTE_TAPE = REPO / "data" / "quotes-intraday.jsonl"  # 1-min sentinel tape: per-symbol last price
SNAPSHOT = REPO / "data" / "tick" / "broker_snapshot.json"
OUT = REPO / "data" / "ledger_truth.json"
ACCOUNT = None  # resolved from rh_direct/.env at runtime
SLIP_WINDOW_SEC = 120  # match an exit fill to the nearest tape price within +/- this many seconds


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fmt_usd(x: float) -> str:
    return f"{'-' if x < 0 else ''}${abs(x):,.2f}"


# ---------------------------------------------------------------- broker truth
def fetch_broker_orders(state: str = "filled") -> list[dict]:
    """All agentic filled orders, newest-first, via the direct (no-LLM) MCP path."""
    import os
    acct = os.environ.get("AGENTIC_ACCOUNT") or _account_from_env()
    if not acct:
        raise SystemExit("no AGENTIC_ACCOUNT in env/.env — cannot reconcile")
    globals()["ACCOUNT"] = acct
    return rh_direct.all_orders(acct, state=state)


def _account_from_env() -> str | None:
    env = REPO / ".env"
    if not env.exists():
        return None
    import re
    m = re.search(r"^AGENTIC_ACCOUNT=(\S+)", env.read_text(), re.M)
    return m.group(1) if m else None


def broker_positions() -> dict[str, float]:
    """Current open positions {sym: qty} from the freshest broker snapshot, if present."""
    if not SNAPSHOT.exists():
        return {}
    try:
        snap = json.loads(SNAPSHOT.read_text())
    except (OSError, ValueError):
        return {}
    pos = {}
    raw = (((snap.get("positions") or {}).get("data") or {}).get("positions")) or []
    for p in raw:
        q = _f(p.get("quantity")) or 0.0
        if abs(q) > 1e-9:
            pos[str(p.get("symbol", "")).upper()] = q
    return pos


# ---------------------------------------------------------------- FIFO over broker fills
def fifo_round_trips(orders: list[dict]) -> tuple[list[dict], dict[str, float]]:
    """Pair sells to buys FIFO per symbol over BROKER-CONFIRMED fills (oldest first).

    Uses cumulative_quantity (what actually executed) and average_price (real VWAP fill). Each closed
    chunk is one round-trip with real realized $. Leftover buys are the reconstructed open book."""
    fills = []
    for o in orders:
        if o.get("state") != "filled":
            continue
        qty = _f(o.get("cumulative_quantity")) or _f(o.get("quantity")) or 0.0
        px = _f(o.get("average_price")) or _f(o.get("price"))
        if qty <= 0 or px is None:
            continue
        ts = o.get("last_transaction_at") or o.get("created_at") or ""
        fills.append({"sym": str(o.get("symbol", "")).upper(), "side": str(o.get("side", "")).lower(),
                      "qty": qty, "px": px, "ts": ts, "id": o.get("id")})
    fills.sort(key=lambda r: r["ts"])  # oldest first for FIFO

    open_lots: dict[str, deque] = defaultdict(deque)
    trips: list[dict] = []
    for r in fills:
        if r["side"] == "buy":
            open_lots[r["sym"]].append([r["qty"], r["px"], r["ts"]])
            continue
        remaining, lots = r["qty"], open_lots[r["sym"]]
        while remaining > 1e-9 and lots:
            lot = lots[0]
            take = min(remaining, lot[0])
            trips.append({
                "symbol": r["sym"], "qty": round(take, 6),
                "entry_price": round(lot[1], 4), "exit_price": round(r["px"], 4),
                "realized_usd": round((r["px"] - lot[1]) * take, 2),
                "pnl_pct": round((r["px"] / lot[1] - 1) * 100, 2) if lot[1] else 0.0,
                "entry_ts": lot[2][:19].replace("T", " "), "exit_ts": r["ts"][:19].replace("T", " "),
                "hold": _hold(lot[2], r["ts"]),
                "exit_order_id": r["id"],
            })
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-9:
                lots.popleft()
        # a sell with no open lot (history older than our page) is dropped from pairing
    open_book = {s: float(round(sum(l[0] for l in lots), 6)) for s, lots in open_lots.items()
                 if sum(l[0] for l in lots) > 1e-9}
    return trips, open_book


def _hold(a: str, b: str) -> str:
    try:
        ta = datetime.fromisoformat(a.replace("Z", "+00:00"))
        tb = datetime.fromisoformat(b.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    m = (tb - ta).total_seconds() / 60
    if m < 0:
        return "?"
    return f"{m:.0f}m" if m < 60 else (f"{m/60:.1f}h" if m < 1440 else f"{m/1440:.1f}d")


# ---------------------------------------------------------------- exit-type enrichment from our log
def exit_types_from_log() -> dict[str, list[tuple[datetime, str]]]:
    """Per-symbol timeline of our recorded sell intents: {SYM: [(utc_dt, exit_type), ...]}.

    The broker record has the fill PRICE; our log has the WHY (stop / scale-out / discretionary).
    We match a broker exit to the nearest log sell for that symbol within a window (see label_trip)
    rather than on an exact minute, because a placed exit and its fill can straddle a minute and the
    'closed_external' reconcile row lands minutes later."""
    out: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    if not TRADES_LOG.exists():
        return out
    for line in TRADES_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("side") != "sell" or r.get("mode") not in ("live", "live-dryrun"):
            continue
        try:
            dt = datetime.fromisoformat((r.get("ts_utc") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        et = r.get("exit_type") or classify_exit(r.get("reason", ""))
        out[str(r.get("symbol", "")).upper()].append((dt, et))
    for v in out.values():
        v.sort(key=lambda x: x[0])
    return out


def label_trip(exit_ts: str, sym: str, log_exits: dict[str, list[tuple[datetime, str]]],
               window_min: float = 15.0) -> str:
    """Best exit_type for a broker round-trip: the log sell for this symbol closest in time to the
    broker fill, within +/- window_min. Falls back to 'other' (discretionary) when nothing matches."""
    try:
        et_dt = datetime.fromisoformat(exit_ts.replace(" ", "T") + "+00:00")
    except ValueError:
        return "other"
    best, best_gap = "other", window_min * 60
    for dt, et in log_exits.get(sym, []):
        gap = abs((dt - et_dt).total_seconds())
        if gap <= best_gap:
            best, best_gap = et, gap
    return best


# ---------------------------------------------------------------- exit slippage (fill vs tape)
def load_quote_tape() -> dict[str, list[tuple[datetime, float]]]:
    """1-min sentinel tape -> {SYM: [(utc_dt, last), ...]} in time order. The tape stores one last
    price per symbol per open-market minute (no bid/ask), so it is a MID-ISH reference, not a true
    mid — slippage below is measured against it and labelled accordingly."""
    out: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    if not QUOTE_TAPE.exists():
        return out
    for line in QUOTE_TAPE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        try:
            dt = datetime.fromisoformat((row.get("ts_utc") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        for sym, last in (row.get("quotes") or {}).items():
            if isinstance(last, (int, float)) and last > 0:
                out[str(sym).upper()].append((dt, float(last)))
    for v in out.values():
        v.sort(key=lambda x: x[0])
    return out


def tape_ref(sym: str, exit_ts: str, tape: dict[str, list[tuple[datetime, float]]],
             window_sec: float = SLIP_WINDOW_SEC) -> float | None:
    """The tape price nearest the exit fill for `sym`, within +/- window_sec. None if no tape sample
    is close enough (symbol never on tape, or the fill fell outside open-market sentinel passes)."""
    try:
        et = datetime.fromisoformat(exit_ts.replace(" ", "T") + "+00:00")
    except ValueError:
        return None
    best, best_gap = None, window_sec
    for dt, px in tape.get(sym, []):
        gap = abs((dt - et).total_seconds())
        if gap <= best_gap:
            best, best_gap = px, gap
    return best


def order_kind(exit_type: str) -> str:
    """How the exit was sent, inferred from exit_type (sell_spec.urgent): a partial scale-out goes as
    a price-protected marketable LIMIT; every full close (stop / take-profit / discretionary 'other')
    goes as a fill-certain MARKET sell. This is the market-vs-limit axis the slippage view compares."""
    return "limit" if exit_type == "scale_out" else "market"


def _slip_stats(rows: list[dict]) -> dict:
    bps = [r["slippage_bps"] for r in rows if r.get("slippage_bps") is not None]
    if not bps:
        return {"n": 0, "mean_bps": None, "median_bps": None}
    return {"n": len(bps), "mean_bps": round(statistics.mean(bps), 1),
            "median_bps": round(statistics.median(bps), 1)}


# ---------------------------------------------------------------- assemble
def reconcile(state: str = "filled") -> dict:
    orders = fetch_broker_orders(state=state)
    trips, open_book = fifo_round_trips(orders)
    bpos = broker_positions()

    # label round-trips with our exit reason where we can match minute-by-minute
    log_exits = exit_types_from_log()
    for t in trips:
        t["exit_type"] = label_trip(t["exit_ts"], t["symbol"], log_exits)

    # exit slippage: how far the real broker fill sat from the 1-min tape price at exit time.
    # >0 = price improvement (sold above the tape ref), <0 = cost (sold below). Inferred order_kind
    # (market full close vs limit scale-out) is the axis the --slippage view compares.
    tape = load_quote_tape()
    for t in trips:
        ref = tape_ref(t["symbol"], t["exit_ts"], tape)
        t["tape_ref"] = round(ref, 4) if ref else None
        t["order_kind"] = order_kind(t["exit_type"])
        t["slippage_bps"] = round((t["exit_price"] / ref - 1.0) * 1e4, 1) if ref else None

    realized = round(sum(t["realized_usd"] for t in trips), 2)
    wins = [t for t in trips if t["realized_usd"] > 1e-9]
    losses = [t for t in trips if t["realized_usd"] < -1e-9]
    gw = sum(t["realized_usd"] for t in wins)
    gl = -sum(t["realized_usd"] for t in losses)

    per_sym = defaultdict(float)
    for t in trips:
        per_sym[t["symbol"]] += t["realized_usd"]
    ex = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
    for t in trips:
        e = t["exit_type"]
        ex[e]["n"] += 1
        ex[e]["pnl"] += t["realized_usd"]
        ex[e]["wins"] += 1 if t["realized_usd"] > 1e-9 else 0

    # drift: reconstructed open book vs the broker's actual open positions
    drift = []
    syms = set(open_book) | set(bpos)
    for s in sorted(syms):
        recon, actual = open_book.get(s, 0.0), bpos.get(s, 0.0)
        if abs(recon - actual) > 1e-6:
            drift.append({"symbol": s, "reconstructed": recon, "broker": actual,
                          "delta": round(recon - actual, 4)})

    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account": ACCOUNT,
        "source": "get_equity_orders (broker-confirmed fills, placed_agent=agentic)",
        "n_orders_filled": sum(1 for o in orders if o.get("state") == "filled"),
        "n_round_trips": len(trips),
        "realized_usd": realized,
        "win_rate": round(100 * len(wins) / len(trips)) if trips else 0,
        "n_wins": len(wins), "n_losses": len(losses),
        "avg_win": round(gw / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gl / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gw / gl, 2) if gl > 1e-9 else None,
        "per_symbol": sorted(({"symbol": k, "realized_usd": round(v, 2)} for k, v in per_sym.items()),
                             key=lambda d: d["realized_usd"]),
        "exit_types": [{"key": k, "label": EXIT_LABEL.get(k, k), "n": v["n"],
                        "realized_usd": round(v["pnl"], 2),
                        "win_pct": round(100 * v["wins"] / v["n"]) if v["n"] else 0}
                       for k, v in sorted(ex.items(), key=lambda kv: kv[1]["pnl"])],
        "open_book_reconstructed": open_book,
        "open_book_broker": bpos,
        "drift": drift,
        "slippage": {
            "window_sec": SLIP_WINDOW_SEC,
            "ref": "1-min sentinel tape last price (mid-ish, no bid/ask); >0 bps = sold above ref",
            "n_matched": sum(1 for t in trips if t.get("slippage_bps") is not None),
            "n_total": len(trips),
            "all": _slip_stats(trips),
            "by_order_kind": {k: _slip_stats([t for t in trips if t["order_kind"] == k])
                              for k in ("market", "limit")},
            "by_exit_type": {e: _slip_stats([t for t in trips if t["exit_type"] == e])
                             for e in sorted({t["exit_type"] for t in trips})},
        },
        "round_trips": sorted(trips, key=lambda t: t["exit_ts"], reverse=True),
    }


# ---------------------------------------------------------------- report
def report(d: dict) -> None:
    print(f"\n{'='*70}\nBROKER-TRUTH RECONCILIATION  ({d['account']})\n{'='*70}")
    print(f"source : {d['source']}")
    print(f"orders : {d['n_orders_filled']} filled · {d['n_round_trips']} round-trips\n")
    print(f"REALIZED P&L : {fmt_usd(d['realized_usd'])}   over {d['n_round_trips']} chunks")
    print(f"  wins {d['n_wins']} / losses {d['n_losses']}   win-rate {d['win_rate']}%")
    print(f"  avg win {fmt_usd(d['avg_win'])}   avg loss {fmt_usd(d['avg_loss'])}   "
          f"profit factor {d['profit_factor'] if d['profit_factor'] is not None else 'n/a'}")

    print(f"\n{'-'*70}\nexit type            n     realized      win%\n{'-'*70}")
    for r in d["exit_types"]:
        print(f"{r['label']:<18}{r['n']:>4}{fmt_usd(r['realized_usd']):>13}{r['win_pct']:>8}%")

    slip = d.get("slippage", {})
    if slip:
        print(f"\n{'-'*70}\nEXIT SLIPPAGE — fill vs 1-min tape ({slip['n_matched']}/{slip['n_total']} "
              f"matched within {slip['window_sec']}s)\n{'-'*70}")
        print("  (>0 = sold ABOVE tape ref / price improvement; <0 = sold below / cost)")

        def _row(label: str, s: dict) -> None:
            if s.get("n"):
                print(f"  {label:<22}n={s['n']:>3}   mean {s['mean_bps']:>+7.1f} bps   "
                      f"median {s['median_bps']:>+7.1f} bps")
            else:
                print(f"  {label:<22}n=  0   (no tape-matched exits)")
        _row("MARKET (full closes)", slip["by_order_kind"]["market"])
        _row("LIMIT (scale-outs)", slip["by_order_kind"]["limit"])
        print(f"  {'-'*60}")
        for e, s in slip["by_exit_type"].items():
            _row(EXIT_LABEL.get(e) or e, s)

    print(f"\n{'-'*70}\nper-symbol net realized ({len(d['per_symbol'])} names)\n{'-'*70}")
    for r in d["per_symbol"]:
        print(f"  {r['symbol']:<7}{fmt_usd(r['realized_usd']):>12}")

    print(f"\n{'-'*70}\nOPEN BOOK — reconstructed vs broker\n{'-'*70}")
    if not d["drift"]:
        print("  ✓ reconstructed open book matches broker positions exactly.")
    else:
        print(f"  ⚠ {len(d['drift'])} mismatch(es) — broker fills don't fully reconstruct the book")
        print("    (older history beyond the fetched pages, or non-agentic fills):")
        for x in d["drift"]:
            print(f"    {x['symbol']:<7} reconstructed {x['reconstructed']:>8}  "
                  f"broker {x['broker']:>8}  Δ{x['delta']:+}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write data/ledger_truth.json")
    ap.add_argument("--json", action="store_true", help="emit raw JSON only")
    ap.add_argument("--state", default="filled", help="order state filter (default filled)")
    args = ap.parse_args()

    try:
        d = reconcile(state=args.state)
    except rh_direct.DirectError as e:
        print(f"broker fetch failed (direct MCP): {e}", file=sys.stderr)
        print("  the direct path needs a fresh keychain token — run a live tick or `claude /mcp` "
              "to refresh, then retry.", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(d, indent=2))
    else:
        report(d)
    if args.write:
        OUT.write_text(json.dumps(d, indent=2))
        print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
