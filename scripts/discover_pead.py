#!/usr/bin/env python3
"""
discover_pead.py — PEAD (post-earnings announcement drift) candidate discovery.

Scrapes the Nasdaq earnings calendar for the last PEAD_LOOKBACK_DAYS calendar days,
filters for eligible names, and returns them so tick_context can prepend them to the
discovery list — giving the DD agent explicit "this stock just had earnings" context.

Sorting priority:
  1. Pre-market today — gap is freshly set at today's open (day 1, highest urgency)
  2. After-hours yesterday — same: gap set at today's open (day 1)
  3. Older reports — still inside the 5-20d drift window, decreasing recency priority

Env vars:
  PEAD_LOOKBACK_DAYS   how many calendar days back to scan (default 4; covers weekend gaps)
  PEAD_MAX_CANDIDATES  cap on returned symbols (default 20)
  MIN_MARKET_CAP_USD   shared with discover.py (default 2_000_000_000)

Cache: one file per ET trading date (data/tick/pead_latest.json); fetching is skipped
       within the same ET day so the calendar doesn't churn on every 5-min tick.

Usage:  python3 scripts/discover_pead.py     # prints candidates, writes cache
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import market_conditions as mc  # sibling: ET, INDEXES

REPO = Path(__file__).resolve().parent.parent
TICK = REPO / "data" / "tick"
CACHE = TICK / "pead_latest.json"

EARNINGS_API = "https://api.nasdaq.com/api/calendar/earnings?date={date}"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_FUND_WORDS = re.compile(
    r"\b(ETF|ETN|FUND|TRUST|2X|3X|ULTRA|ULTRAPRO|INVERSE|BULL|BEAR|LEVERAGED)\b", re.I)


# --------------------------------------------------------------------------- helpers

def _env(k: str, d: str) -> str:
    v = os.environ.get(k)
    return v if v not in (None, "") else d


def _envf(k: str, d: float) -> float:
    try:
        return float(_env(k, str(d)))
    except ValueError:
        return d


def _envi(k: str, d: int) -> int:
    try:
        return int(float(_env(k, str(d))))
    except ValueError:
        return d


def _num(x) -> float | None:
    try:
        return float(re.sub(r"[%$,]", "", str(x)))
    except (TypeError, ValueError):
        return None


def _exclude_set() -> set[str]:
    extra = {s.strip().upper() for s in _env("NON_TRADABLE_SYMBOLS", "").split(",") if s.strip()}
    try:
        import stock_memory
        extra |= stock_memory.excluded_symbols()
    except Exception:
        pass
    return set(mc.INDEXES) | extra


def _trading_dates_back(n_trading_days: int) -> list[date]:
    """Return the last n_trading_days weekday dates (Mon-Fri) going back from today inclusive."""
    today = datetime.now(mc.ET).date()
    out = []
    d = today
    while len(out) < n_trading_days:
        if d.weekday() < 5:  # Mon-Fri
            out.append(d)
        d -= timedelta(days=1)
    return out


# --------------------------------------------------------------------------- fetch

def _fetch_calendar(dt: date) -> list[dict]:
    """Fetch Nasdaq earnings calendar rows for one date. [] on failure."""
    url = EARNINGS_API.format(date=dt.strftime("%Y-%m-%d"))
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json",
        "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        sys.stderr.write(f"[discover_pead] calendar fetch failed ({dt}): {e}\n")
        return []
    rows = ((d.get("data") or {}).get("rows")) or []
    return rows


def _eligible_row(row: dict, min_mktcap: float, exclude: set[str]) -> bool:
    sym = (row.get("symbol") or "").strip().upper()
    if not sym or sym in exclude:
        return False
    if any(ch in sym for ch in ("^", "/", ".")):
        return False
    if _FUND_WORDS.search(row.get("name") or ""):
        return False
    mktcap = _num(row.get("marketCap"))
    if mktcap is None or mktcap < min_mktcap:
        return False
    return True


# --------------------------------------------------------------------------- sort key

def _sort_priority(meta: dict) -> int:
    """Lower = higher priority. Day-0 pre-market = 0, day-0 after-hours = 1, older = 2+."""
    days = meta.get("days_since", 99)
    timing = meta.get("time", "")
    if days == 0 and "pre" in timing:
        return 0
    if days <= 1 and "after" in timing:
        return 1
    return 2 + days


# --------------------------------------------------------------------------- public API

def pead_meta(force_refresh: bool = False) -> dict[str, dict]:
    """
    Return {symbol: {earnings_date, time, days_since}} for all PEAD candidates.

    Cached per ET trading day — safe to call every tick.
    """
    today_et = datetime.now(mc.ET).strftime("%Y-%m-%d")

    if not force_refresh:
        try:
            cached = json.loads(CACHE.read_text())
            if cached.get("date") == today_et and cached.get("meta"):
                return cached["meta"]
        except (OSError, ValueError):
            pass

    lookback = _envi("PEAD_LOOKBACK_DAYS", 4)
    min_mktcap = _envf("MIN_MARKET_CAP_USD", 2_000_000_000.0)
    exclude = _exclude_set()
    today_date = datetime.now(mc.ET).date()

    meta: dict[str, dict] = {}
    for dt in _trading_dates_back(lookback):
        rows = _fetch_calendar(dt)
        days_since = (today_date - dt).days
        for row in rows:
            sym = (row.get("symbol") or "").strip().upper()
            if not _eligible_row(row, min_mktcap, exclude):
                continue
            if sym in meta:
                continue  # keep the most recent report per symbol
            timing = (row.get("time") or "time-not-supplied").strip()
            meta[sym] = {
                "earnings_date": dt.strftime("%Y-%m-%d"),
                "time": timing,
                "days_since": days_since,
                "name": (row.get("name") or "").strip(),
                "eps_forecast": (row.get("epsForecast") or "").strip(),
                # mktcap feeds the two-book router (pead book = mega-cap only; v2 plan): the
                # calendar row is the only keyless mktcap source for names the gainer screen
                # didn't also surface.
                "mktcap": _num(row.get("marketCap")),
            }

    # Write cache
    TICK.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "date": today_et,
        "fetched_ts": datetime.now(mc.ET).isoformat(timespec="seconds"),
        "n": len(meta),
        "meta": meta,
    }, indent=2))
    import os as _os; _os.replace(tmp, CACHE)

    return meta


def discover_pead(max_candidates: int | None = None) -> list[str]:
    """
    List of PEAD candidate symbols, sorted by drift-window urgency.

    Drop-in alongside discover.discover() — same return type (list[str]).
    """
    max_n = max_candidates if max_candidates is not None else _envi("PEAD_MAX_CANDIDATES", 20)
    meta = pead_meta()
    ranked = sorted(meta.keys(), key=lambda s: _sort_priority(meta[s]))
    return ranked[:max_n]


# --------------------------------------------------------------------------- CLI

def main() -> int:
    t0 = time.time()
    meta = pead_meta(force_refresh=True)
    ranked = sorted(meta.keys(), key=lambda s: _sort_priority(meta[s]))
    print(f"PEAD candidates: {len(ranked)} found in {time.time()-t0:.1f}s")
    for sym in ranked:
        m = meta[sym]
        print(f"  {sym:6}  {m['earnings_date']} {m['time'].replace('time-',''):12}  "
              f"day+{m['days_since']}  {m['name'][:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
