#!/usr/bin/env python3
"""
discover.py — dynamic momentum discovery universe (keyless), replacing a static stock allowlist.

Pulls the Nasdaq stock screener (all ~7k US equities with intraday % change + price + market cap +
volume, keyless) and applies a rules-based ELIGIBILITY FILTER — the real safety layer a name
allowlist was only crudely faking — then returns the day's top GAINERS that clear it. That pool is
what the deterministic screen (rel-strength vs SPY) and Stage-2 DD then work on.

Why a filter and not a whitelist: the protective stop is SYNTHETIC (engine-tick, ~5 min, no gap
cover), so it gets run over on low-float pumps, micro-caps, and just-IPO'd names — exactly what raw
"top movers" surfaces. Price / market-cap / $-volume / IPO-age floors keep us in names the stop can
actually protect, while still letting ANY qualifying name in (not a fixed list).

The screener is delayed/snapshotted (refreshes every few minutes) — fine: it only decides WHICH
names to look at; live Cboe/MCP quotes price the entry. We cache the snapshot per tick
(data/tick/discovery_latest.json) and, on a fetch failure, fall back to the last-known-good snapshot
so a blip never blanks the universe.

Usage:  python3 scripts/discover.py            # prints the eligible movers, writes the cache
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import market_conditions as mc  # sibling: ET, INDEXES

REPO = Path(__file__).resolve().parent.parent
TICK = REPO / "data" / "tick"
CACHE = TICK / "discovery_latest.json"
SCREENER = ("https://api.nasdaq.com/api/screener/stocks"
            "?tableonly=true&limit=10000&offset=0&download=true")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# Fund / leveraged / inverse name markers — never want these in a single-name momentum book.
_FUND_WORDS = re.compile(r"\b(ETF|ETN|FUND|TRUST|2X|3X|ULTRA|ULTRAPRO|INVERSE|BULL|BEAR|LEVERAGED)\b", re.I)


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
    """Parse '$1,234.50' / '12.3%' / '5,336,584,000,000' -> float; None if unparseable/blank."""
    try:
        return float(re.sub(r"[%$,]", "", str(x)))
    except (TypeError, ValueError):
        return None


def fetch_screener() -> list[dict]:
    """All US-equity rows from the keyless Nasdaq screener. [] on failure (caller falls back)."""
    req = urllib.request.Request(SCREENER, headers={
        "User-Agent": UA, "Accept": "application/json",
        "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        sys.stderr.write(f"[discover] screener fetch failed: {e}\n")
        return []
    data = d.get("data") or {}
    return data.get("rows") or (data.get("table") or {}).get("rows") or []


def eligible(rows: list[dict], *, min_price: float, min_mktcap: float, min_dollar_vol: float,
             exclude_recent_ipo: bool, exclude: set[str], this_year: int) -> list[dict]:
    """Apply the eligibility filter; return ranked gainer dicts {symbol, pct, price, mktcap, dollar_vol}."""
    out = []
    for r in rows:
        sym = (r.get("symbol") or "").strip().upper()
        if not sym or sym in exclude:
            continue
        if any(ch in sym for ch in ("^", "/", ".")):   # warrants / units / pfd / odd classes
            continue
        if _FUND_WORDS.search(r.get("name") or ""):     # drop ETFs / leveraged / inverse / funds
            continue
        px, pct = _num(r.get("lastsale")), _num(r.get("pctchange"))
        mktcap, vol = _num(r.get("marketCap")), _num(r.get("volume"))
        if px is None or pct is None or mktcap is None:
            continue
        if pct <= 0:                                    # long-only momentum: gainers only
            continue
        if px < min_price or mktcap < min_mktcap:
            continue
        dollar_vol = px * vol if (vol is not None) else None
        if min_dollar_vol > 0 and (dollar_vol is None or dollar_vol < min_dollar_vol):
            continue
        if exclude_recent_ipo:
            iy = _num(r.get("ipoyear"))
            if iy is not None and iy >= this_year:      # IPO'd this calendar year — too green
                continue
        out.append({"symbol": sym, "pct": round(pct, 2), "price": px,
                    "mktcap": mktcap, "dollar_vol": round(dollar_vol) if dollar_vol else None})
    out.sort(key=lambda d: -d["pct"])                   # rank by % gain
    return out


def _read_cache() -> dict | None:
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return None


def _write_cache(ts: float, syms: list[str], detail: list[dict]) -> None:
    rec = {"ts": ts, "fetched_ts_et": datetime.now(mc.ET).isoformat(timespec="seconds"),
           "n_eligible": len(detail), "symbols": syms, "detail": detail[:50]}
    TICK.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=2))
    os.replace(tmp, CACHE)


def _exclude_set() -> set[str]:
    """Never-surface names: index-ETF benchmarks + NON_TRADABLE_SYMBOLS + the long-term never-buy list."""
    extra = {s.strip().upper() for s in _env("NON_TRADABLE_SYMBOLS", "").split(",") if s.strip()}
    try:
        import stock_memory
        extra |= stock_memory.excluded_symbols()
    except Exception:
        pass
    return set(mc.INDEXES) | extra


def discover(max_candidates: int | None = None) -> list[str]:
    """Top eligible gainer symbols, cached per tick with last-known-good fallback.

    Returns up to MAX_DISCOVERY_CANDIDATES symbols. Never raises — on total failure with no cache it
    returns [] and the caller falls back to the pinned CANDIDATES list.
    """
    max_n = max_candidates if max_candidates is not None else _envi("MAX_DISCOVERY_CANDIDATES", 25)
    ttl = _envf("DISCOVERY_CACHE_MIN", 5.0) * 60
    now = time.time()
    cached = _read_cache()
    if cached and (now - cached.get("ts", 0)) < ttl and cached.get("symbols"):
        return cached["symbols"][:max_n]

    rows = fetch_screener()
    if rows:
        elig = eligible(
            rows,
            min_price=_envf("MIN_PRICE", 5.0),
            min_mktcap=_envf("MIN_MARKET_CAP_USD", 2_000_000_000.0),
            min_dollar_vol=_envf("MIN_DOLLAR_VOL", 20_000_000.0),
            exclude_recent_ipo=_env("EXCLUDE_RECENT_IPO", "1") == "1",
            exclude=_exclude_set(),
            this_year=datetime.now(mc.ET).year,
        )
        syms = [d["symbol"] for d in elig]
        _write_cache(now, syms, elig)
        return syms[:max_n]

    if cached and cached.get("symbols"):   # fetch failed -> last-known-good beats a blank universe
        sys.stderr.write("[discover] screener fetch failed; using stale discovery cache\n")
        return cached["symbols"][:max_n]
    return []


def main() -> int:
    syms = discover()
    cached = _read_cache() or {}
    detail = {d["symbol"]: d for d in cached.get("detail", [])}
    print(f"discovery: {len(cached.get('symbols', []))} eligible gainers "
          f"(fetched {cached.get('fetched_ts_et', '?')}); top {len(syms)} surfaced:")
    for s in syms:
        d = detail.get(s, {})
        print(f"  {s:6} +{d.get('pct')}%  ${d.get('price')}  "
              f"mktcap=${(d.get('mktcap') or 0)/1e9:,.1f}B  $vol=${(d.get('dollar_vol') or 0)/1e6:,.0f}M")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
