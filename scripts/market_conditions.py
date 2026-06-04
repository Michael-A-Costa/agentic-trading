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
import urllib.parse
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
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/{sym}.json"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1d&interval=1d"
HTTP_TIMEOUT = 12
# A browser-ish UA helps the Yahoo fallback (anonymous chart API 429s bare clients).
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _fnum(v) -> float | None:
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- fetch: primary (Stooq)
def fetch_stooq(symbols: list[str]) -> dict[str, dict]:
    """{SYMBOL: {open,high,low,last,volume,date,time}} from Stooq's keyless batch CSV (one request)."""
    syms = "+".join(f"{s.lower()}.us" for s in symbols)
    text = _http_get(STOOQ_URL.format(syms=syms))
    out: dict[str, dict] = {}
    for row in csv.DictReader(io.StringIO(text)):
        sym = (row.get("Symbol") or "").split(".")[0].upper()
        if not sym:
            continue
        out[sym] = {
            "open": _fnum(row.get("Open")),
            "high": _fnum(row.get("High")),
            "low": _fnum(row.get("Low")),
            "last": _fnum(row.get("Close")),  # Stooq "Close" is the latest print intraday
            "volume": _fnum(row.get("Volume")),
            "date": (row.get("Date") or "").strip(),
            "time": (row.get("Time") or "").strip(),
        }
    return out


# --------------------------------------------------------------------------- fetch: fallback 1 (Cboe)
def fetch_cboe(symbols: list[str]) -> dict[str, dict]:
    """Same shape, from Cboe's keyless delayed-quotes JSON (one request per symbol)."""
    out: dict[str, dict] = {}
    for s in symbols:
        try:
            d = json.loads(_http_get(CBOE_URL.format(sym=urllib.parse.quote(s.upper()))))
            data = d.get("data") or {}
            if not data:
                continue
            date, _, tm = (d.get("timestamp") or "").strip().partition(" ")
            out[s.upper()] = {
                "open": _fnum(data.get("open")),
                "high": _fnum(data.get("high")),
                "low": _fnum(data.get("low")),
                "last": _fnum(data.get("current_price") if data.get("current_price") is not None
                              else data.get("close")),
                "volume": _fnum(data.get("volume")),
                "date": date,
                "time": tm,
            }
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
            continue
    return out


# --------------------------------------------------------------------------- fetch: fallback 2 (Yahoo)
def fetch_yahoo(symbols: list[str]) -> dict[str, dict]:
    """Same shape, from Yahoo's keyless chart API (one request per symbol). Used only if Stooq fails."""
    out: dict[str, dict] = {}
    for s in symbols:
        try:
            data = json.loads(_http_get(YAHOO_URL.format(sym=urllib.parse.quote(s))))
            res = (data.get("chart", {}).get("result") or [None])[0]
            if not res:
                continue
            meta = res.get("meta", {})
            q = (res.get("indicators", {}).get("quote") or [{}])[0]
            opens = [x for x in (q.get("open") or []) if x is not None]
            ts = meta.get("regularMarketTime")
            when = datetime.fromtimestamp(ts, ET) if ts else None
            out[s.upper()] = {
                "open": _fnum(opens[-1]) if opens else _fnum(meta.get("chartPreviousClose")),
                "high": _fnum(meta.get("regularMarketDayHigh")),
                "low": _fnum(meta.get("regularMarketDayLow")),
                "last": _fnum(meta.get("regularMarketPrice")),
                "volume": _fnum(meta.get("regularMarketVolume")),
                "date": when.strftime("%Y-%m-%d") if when else "",
                "time": when.strftime("%H:%M:%S") if when else "",
            }
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
            continue  # skip this symbol; orchestrator decides if the whole fetch failed
    return out


def _has_indexes(quotes: dict[str, dict]) -> bool:
    """A usable fetch must have a last price for at least one index ETF."""
    return any(quotes.get(s, {}).get("last") is not None for s in INDEXES)


# --------------------------------------------------------------------------- fetch: orchestrator
def fetch_quotes(symbols: list[str]) -> tuple[dict[str, dict], str]:
    """Try sources in order until one returns index data. Returns (quotes, source). Raises if all fail.

    Chain: Stooq (primary, batch + 1 retry) -> Cboe (fallback 1) -> Yahoo (fallback 2). All three are
    independent keyless providers, so a single provider's outage or throttle doesn't blind the engine.
    """
    last_err: Exception | None = None
    attempts = [
        ("stooq", fetch_stooq),
        ("stooq(retry)", fetch_stooq),
        ("cboe(fallback)", fetch_cboe),
        ("yahoo(fallback)", fetch_yahoo),
    ]
    for label, fn in attempts:
        try:
            q = fn(symbols)
            if _has_indexes(q):
                return q, label
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
    raise RuntimeError(f"all sources failed (last error: {last_err})")


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
    source = None
    quotes: dict[str, dict] = {}
    try:
        quotes, source = fetch_quotes(ALL_SYMBOLS)
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as e:
        error = f"fetch_failed: {e}"

    record = {
        "ts_utc": now_utc.isoformat(timespec="seconds"),
        "ts_et": now_et.isoformat(timespec="seconds"),
        "session": session,
        "market_open": is_open,
        "source": source,
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
        f"[{record['ts_et']}] {record['session'].upper()} src={record.get('source')} | "
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
