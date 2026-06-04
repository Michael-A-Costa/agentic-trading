#!/usr/bin/env python3
"""
market_conditions.py — headless market-regime checker for the agentic-trading engine.

Read-only. No Robinhood MCP, no account access, no orders. Pulls a basket of index
ETFs + a VIX proxy from Stooq's keyless CSV endpoint, classifies the session's risk
posture, prints a one-line summary, and appends a structured record to a JSONL log.

Why public data (not the Robinhood MCP): the MCP is authed through the interactive
Claude client and can be absent in a headless/cron run. Market-regime data is public,
so this stays a self-contained, dependency-free Python job that runs anywhere on cron.

Stdlib only (urllib, json, csv, zoneinfo) so there is nothing to pip-install on the box.

Usage:
    python3 scripts/market_conditions.py            # check, log, print summary
    python3 scripts/market_conditions.py --json     # emit the full record as JSON
    python3 scripts/market_conditions.py --quiet     # log only, no stdout summary

Exit code is 0 on a successful check (even when the market is closed); non-zero only on
a hard data-fetch failure (which is also logged).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parent.parent
LOG_PATH = REPO / "data" / "market_conditions.jsonl"

# Index ETFs that define the broad-market read, plus VIXY as a fear proxy (VIX itself
# isn't available keyless). VIXY is an ETF — we read its *direction*, not an absolute level.
INDEXES = ["SPY", "QQQ", "IWM", "DIA"]
VIX_PROXY = "VIXY"
ALL_SYMBOLS = INDEXES + [VIX_PROXY]

STOOQ_URL = "https://stooq.com/q/l/?s={syms}&f=sd2t2ohlcv&h&e=csv"
HTTP_TIMEOUT = 12
UA = "Mozilla/5.0 (agentic-trading market_conditions.py)"


# --------------------------------------------------------------------------- fetch
def fetch_quotes(symbols: list[str]) -> dict[str, dict]:
    """Return {SYMBOL: {open, high, low, last, volume, date, time}} from Stooq (keyless)."""
    syms = "+".join(f"{s.lower()}.us" for s in symbols)
    url = STOOQ_URL.format(syms=syms)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        text = resp.read().decode("utf-8", errors="replace")

    out: dict[str, dict] = {}
    for row in csv.DictReader(io.StringIO(text)):
        sym = (row.get("Symbol") or "").split(".")[0].upper()
        if not sym:
            continue

        def num(key):
            v = (row.get(key) or "").strip()
            try:
                return float(v)
            except ValueError:
                return None

        out[sym] = {
            "open": num("Open"),
            "high": num("High"),
            "low": num("Low"),
            "last": num("Close"),  # Stooq "Close" is the latest print intraday
            "volume": num("Volume"),
            "date": (row.get("Date") or "").strip(),
            "time": (row.get("Time") or "").strip(),
        }
    return out


# --------------------------------------------------------------------------- session
def session_state(now_et: datetime) -> tuple[str, bool]:
    """Crude US-equity session label. Weekday + clock only — does NOT know market holidays."""
    if now_et.weekday() >= 5:
        return "closed_weekend", False
    t = now_et.time()
    if time(4, 0) <= t < time(9, 30):
        return "pre_market", False
    if time(9, 30) <= t < time(16, 0):
        return "regular", True
    if time(16, 0) <= t < time(20, 0):
        return "after_hours", False
    return "closed", False


# --------------------------------------------------------------------------- assess
def intraday_pct(q: dict) -> float | None:
    o, last = q.get("open"), q.get("last")
    if o and last and o != 0:
        return round((last - o) / o * 100, 3)
    return None


def range_position(q: dict) -> float | None:
    """Where last sits in the day's range: 0 = at low, 1 = at high."""
    hi, lo, last = q.get("high"), q.get("low"), q.get("last")
    if hi is not None and lo is not None and last is not None and hi != lo:
        return round((last - lo) / (hi - lo), 3)
    return None


def assess(quotes: dict[str, dict]) -> dict:
    moves = {s: intraday_pct(quotes[s]) for s in INDEXES if s in quotes}
    valid = {s: m for s, m in moves.items() if m is not None}

    green = sum(1 for m in valid.values() if m > 0)
    total = len(valid)
    avg_abs = round(sum(abs(m) for m in valid.values()) / total, 3) if total else None
    avg_move = round(sum(valid.values()) / total, 3) if total else None

    vix_q = quotes.get(VIX_PROXY, {})
    vix_move = intraday_pct(vix_q)  # VIXY up => fear up

    # Volatility regime — from index range magnitude + VIX-proxy direction.
    if avg_abs is None:
        vol = "unknown"
    elif (avg_abs > 1.2) or (vix_move is not None and vix_move > 8):
        vol = "elevated"
    elif (avg_abs < 0.4) and (vix_move is None or vix_move < 2):
        vol = "calm"
    else:
        vol = "normal"

    # Breadth regime.
    if total == 0:
        breadth = "unknown"
    elif green == total:
        breadth = "broad_risk_on"
    elif green >= max(1, total - 1):
        breadth = "leaning_up"
    elif green == 0:
        breadth = "broad_risk_off"
    elif green <= 1:
        breadth = "leaning_down"
    else:
        breadth = "mixed"

    # Overall posture heuristic (clearly a heuristic; the engine consumes `posture`).
    reasons = []
    posture = "neutral"
    if total:
        if green >= 3 and (vix_move is None or vix_move < 2) and (avg_move or 0) > 0:
            posture = "risk_on"
            reasons.append(f"{green}/{total} indexes green, VIX proxy soft")
        elif green <= 1 and (vix_move is not None and vix_move > 3):
            posture = "risk_off"
            reasons.append(f"only {green}/{total} green, VIX proxy +{vix_move}%")
        else:
            reasons.append(f"{green}/{total} green, vol {vol}")
    if vol == "elevated":
        reasons.append("elevated volatility — size down / widen stops")

    return {
        "posture": posture,
        "volatility_regime": vol,
        "breadth_regime": breadth,
        "breadth_green": green,
        "breadth_total": total,
        "avg_index_move_pct": avg_move,
        "avg_abs_move_pct": avg_abs,
        "vix_proxy_move_pct": vix_move,
        "index_moves_pct": valid,
        "reasons": reasons,
    }


# --------------------------------------------------------------------------- trend (self-accumulated)
def trailing_trend(today_close: float | None, lookback: int = 20) -> dict:
    """Use our own JSONL history of SPY closes to compute a simple trend, once enough days exist."""
    if not LOG_PATH.exists() or today_close is None:
        return {"available": False, "note": "insufficient history (self-accumulating)"}
    seen: dict[str, float] = {}
    try:
        for line in LOG_PATH.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            d = rec.get("quotes", {}).get("SPY", {}).get("date")
            c = rec.get("quotes", {}).get("SPY", {}).get("last")
            if d and c:
                seen[d] = c  # last record per session date wins
    except (json.JSONDecodeError, OSError):
        return {"available": False, "note": "history unreadable"}
    closes = [seen[d] for d in sorted(seen)][-lookback:]
    if len(closes) < 5:
        return {"available": False, "note": f"only {len(closes)} session(s) logged; need >=5"}
    ma = round(sum(closes) / len(closes), 2)
    return {
        "available": True,
        "spy_last": today_close,
        f"spy_ma{len(closes)}": ma,
        "above_ma": today_close > ma,
        "trend": "up" if today_close > ma else "down",
        "sessions_used": len(closes),
    }


# --------------------------------------------------------------------------- main
def build_record() -> dict:
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(ET)
    session, is_open = session_state(now_et)

    error = None
    quotes: dict[str, dict] = {}
    try:
        quotes = fetch_quotes(ALL_SYMBOLS)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        error = f"fetch_failed: {e}"

    record = {
        "ts_utc": now_utc.isoformat(timespec="seconds"),
        "ts_et": now_et.isoformat(timespec="seconds"),
        "session": session,
        "market_open": is_open,
        "quotes": quotes,
    }
    if error:
        record["error"] = error
        record["assessment"] = None
        record["trend"] = {"available": False}
    else:
        record["assessment"] = assess(quotes)
        record["trend"] = trailing_trend(quotes.get("SPY", {}).get("last"))
    return record


def append_log(record: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")


def summarize(record: dict) -> str:
    if record.get("error"):
        return f"[{record['ts_et']}] {record['session'].upper()} — DATA ERROR: {record['error']}"
    a = record["assessment"]
    moves = " ".join(f"{s}{'+' if m >= 0 else ''}{m}%" for s, m in a["index_moves_pct"].items())
    vix = a["vix_proxy_move_pct"]
    vix_s = f"VIXY{'+' if (vix or 0) >= 0 else ''}{vix}%" if vix is not None else "VIXY n/a"
    trend = record["trend"]
    trend_s = (f"trend {trend['trend']} (SPY {trend['spy_last']} vs MA{trend['sessions_used']})"
               if trend.get("available") else f"trend: {trend.get('note', 'n/a')}")
    return (
        f"[{record['ts_et']}] {record['session'].upper()} | "
        f"POSTURE={a['posture'].upper()} vol={a['volatility_regime']} breadth={a['breadth_regime']} "
        f"({a['breadth_green']}/{a['breadth_total']}) | {moves} {vix_s} | {trend_s}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Headless market-conditions checker (read-only).")
    ap.add_argument("--json", action="store_true", help="print the full record as JSON")
    ap.add_argument("--quiet", action="store_true", help="log only, suppress summary")
    args = ap.parse_args()

    record = build_record()
    append_log(record)

    if args.json:
        print(json.dumps(record, indent=2))
    elif not args.quiet:
        print(summarize(record))

    return 1 if record.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
