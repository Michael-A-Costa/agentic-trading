#!/usr/bin/env python3
"""
dd_probe.py — deep, script-side due-diligence on ONE candidate before committing capital.

Runs only in Stage 2 (when the cheap screen flags a real entry candidate), so cost is bounded.
Gathers quantitative DD from public sources — NO LLM tokens spent here — and writes a compact
JSON the Stage-2 commit model (DD_MODEL, default Sonnet, with web news search) then judges.

PRIMARY signals (keyless, from Cboe live quote — reliable, always computed):
  - intraday move %, gap %, range position (where in today's H-L we sit; 1.0 = at the high)
  - liquidity: today's $-volume and bid/ask spread %  — the edge must clear the spread
  - IV30
  - flags: spread_ok, liquid, iv_ok, parabolic  (these are the real gate)

HISTORY signals (keyless daily OHLCV from Cboe's CDN charts endpoint — same provider as the live
quote, deep history, no key; Yahoo is a 429-prone fallback). Enrichment, not a hard requirement:
  - multi-timeframe trend: 1/5/20-day returns, vs 20/50-day MA, distance from 3-mo high/low
  - relative volume — PACE-adjusted (today's partial volume vs the same fraction of a normal day)
  - 20-day realized vol
  - flags: trend_up, volume_confirmed, extended, at_high

`history_ok` says whether the bonus block is real. When history is missing, the bonus flags are
**null (unknown), never false** — a data blackout must not masquerade as weak momentum. A true
data problem (no live price/liquidity at all) sets `error` so the commit model rejects on H1.

Usage:  python3 scripts/dd_probe.py META            # prints JSON, also writes data/tick/dd_META.json
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import market_conditions as mc  # sibling: _http_get, _fnum, CBOE_URL, ET, session_state

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
TICK = DATA / "tick"
# The tick writes the quotes it already fetched here; we reuse them so N parallel DD probes don't each
# re-hit Cboe and trip its rate limit. Stale/missing -> live fetch (e.g. standalone `dd_probe.py SYM`).
QUOTES_FILE = TICK / "quotes_latest.json"
QUOTE_FILE_MAX_AGE_S = int(os.environ.get("DD_QUOTE_FILE_MAX_AGE_S", "180"))
HISTORY_DIR = DATA / "history"   # per-symbol daily-bar cache: data/history/{SYM}.json
# Daily history is KEYLESS again via Cboe's CDN (same provider as the live quote): deep OHLCV,
# no key, no throttling. Yahoo stays only as a last-resort fallback (it's usually 429-throttled).
CBOE_HIST = "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/{sym}.json"
YH = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=3mo&interval=1d"
HIST_DAYS = 66   # keep only ~3 trading months, so MA20/50, 20d-vol, and the 3-MO high/low are
                 # computed over a 3-month window (the CDN returns full history back to IPO).

# Keyless quality thresholds (intraday signals from Cboe — no daily history needed). Tunable.
LIQ_FLOOR_USD = 5_000_000   # >= $5M traded today = liquid enough for our small paper sizes
IV_CEIL = 80.0              # iv30 above this = too jumpy for a ~5-min synthetic stop to protect
PARABOLIC_GAP_PCT = 8.0     # gapped >8% at the open = blow-off / chase risk
PARABOLIC_MOVE_PCT = 5.0    # >5% intraday AND pinned at the day's high = extended chase


def pct(a: float, b: float) -> float | None:
    return round((a / b - 1) * 100, 2) if (a is not None and b) else None


def session_fraction() -> float:
    """Fraction of the 9:30–16:00 ET regular session elapsed (clamped to [0.05, 1.0]).

    Returns 1.0 outside regular hours. Used to PACE today's partial cumulative volume against the
    20-day average: comparing a half-day's volume to a full-day average understates it ~2x, which
    would wrongly read 'weak volume' on a name actually trading hot — so we scale the average down
    by how much of the session has elapsed.
    """
    now_et = datetime.now(mc.ET)
    _, is_open = mc.session_state(now_et)
    if not is_open:
        return 1.0
    mins = (now_et.hour * 60 + now_et.minute) - (9 * 60 + 30)
    return max(0.05, min(1.0, mins / 390.0))


def _fetch_cboe_history(sym: str) -> dict:
    """Last ~3 months of daily {closes, volumes} from Cboe's CDN (keyless). {} on failure.

    The endpoint returns full history (back to IPO) oldest->newest; we keep only the last HIST_DAYS
    bars so the 3-mo-high/low and moving-average windows mean what they say. Each bar that lacks a
    close OR a volume is dropped as a pair, so closes[] and volumes[] stay index-aligned.
    """
    try:
        d = json.loads(mc._http_get(CBOE_HIST.format(sym=urllib.parse.quote(sym.upper()))))
        bars = d.get("data") or []
        pairs = [(mc._fnum(b.get("close")), mc._fnum(b.get("volume"))) for b in bars]
        pairs = [(c, v) for (c, v) in pairs if c is not None and v is not None][-HIST_DAYS:]
        return {"closes": [c for c, _ in pairs], "volumes": [v for _, v in pairs]} if pairs else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def yahoo_history(sym: str) -> dict:
    """Fallback daily history from Yahoo (keyless, but frequently 429-throttled). {} on failure."""
    try:
        d = json.loads(mc._http_get(YH.format(sym=urllib.parse.quote(sym))))
        res = (d.get("chart", {}).get("result") or [None])[0]
        if not res:
            return {}
        q = (res.get("indicators", {}).get("quote") or [{}])[0]
        closes = [c for c in (q.get("close") or []) if c is not None]
        vols = [v for v in (q.get("volume") or []) if v is not None]
        return {"closes": closes, "volumes": vols}
    except (OSError, ValueError, KeyError):
        return {}


def _read_history_cache(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_history_cache(path: Path, sym: str, hist: dict, src: str, today: str) -> None:
    """Atomically persist a symbol's daily bars (temp + os.replace, so a crash can't truncate)."""
    rec = {"symbol": sym, "source": src, "fetched_date": today,
           "fetched_ts_et": datetime.now(mc.ET).isoformat(timespec="seconds"),
           "n_bars": len(hist["closes"]),
           "closes": hist["closes"], "volumes": hist["volumes"]}
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec))
    os.replace(tmp, path)


def load_history(sym: str) -> tuple[dict, str | None]:
    """Daily history for trend/MA/volume, cached per symbol in data/history/{SYM}.json.

    Daily bars only finalize after the close, so one fetch per symbol per ET day is enough — a cache
    written today is served as-is. On a stale/missing cache we fetch (Cboe CDN primary, Yahoo
    fallback) and rewrite the file. If every live fetch fails we fall back to the stale cache, so a
    brief Cboe outage doesn't blind DD. Returns (hist, source_label).
    """
    sym = sym.upper()
    today = datetime.now(mc.ET).strftime("%Y-%m-%d")
    path = HISTORY_DIR / f"{sym}.json"
    cached = _read_history_cache(path)
    if cached and cached.get("fetched_date") == today and cached.get("closes"):
        return {"closes": cached["closes"], "volumes": cached["volumes"]}, cached.get("source")

    hist, src = _fetch_cboe_history(sym), "cboe"
    if not hist.get("closes"):
        hist, src = yahoo_history(sym), "yahoo"
    if hist.get("closes"):
        _write_history_cache(path, sym, hist, src, today)
        return hist, src

    if cached and cached.get("closes"):   # live fetch failed -> last-known-good beats blind
        return {"closes": cached["closes"], "volumes": cached["volumes"]}, (cached.get("source") or "cache")
    return {}, None


def _quote_from_tick_cache(sym: str) -> dict | None:
    """The quote the tick already fetched this cycle, if fresh and present. Reused so parallel DD
    processes don't each re-hit Cboe (that concurrent burst is what 429s -> no_live_quote). The cached
    quote carries the same raw-Cboe keys probe() reads (current_price/bid/ask/iv30/...); see
    market_conditions.fetch_cboe."""
    try:
        blob = json.loads(QUOTES_FILE.read_text())
    except (OSError, ValueError):
        return None
    if time.time() - (blob.get("ts") or 0) >= QUOTE_FILE_MAX_AGE_S:
        return None  # stale (e.g. a standalone run hours later) -> fetch live instead
    q = (blob.get("quotes") or {}).get(sym.upper())
    # require a real price; a non-Cboe source (stooq/yahoo) lacks current_price -> fetch live
    return q if (q and q.get("current_price") is not None) else None


def cboe_quote(sym: str) -> dict:
    cached = _quote_from_tick_cache(sym)
    if cached is not None:
        return cached
    try:
        d = json.loads(mc._http_get(mc.CBOE_URL.format(sym=urllib.parse.quote(sym.upper()))))
        return d.get("data") or {}
    except (OSError, ValueError, KeyError):
        return {}


def probe(sym: str) -> dict:
    sym = sym.upper()
    hist, hist_src = load_history(sym)
    cb = cboe_quote(sym)
    closes = hist.get("closes") or []
    vols = hist.get("volumes") or []

    # history_ok drives the rubric: when false, daily trend/volume flags are null (unknown), NOT
    # false — a data blackout must never read as 'weak momentum'. Cboe intraday is the primary source.
    out: dict = {"symbol": sym, "sources": {"history": hist_src or False, "cboe_quote": bool(cb)},
                 "history_ok": bool(closes)}

    # --- live quote / liquidity / gap / intraday structure (Cboe — keyless, always-on) ---
    last = mc._fnum(cb.get("current_price")) or (closes[-1] if closes else None)
    bid, ask = mc._fnum(cb.get("bid")), mc._fnum(cb.get("ask"))
    prev_close = mc._fnum(cb.get("prev_day_close"))
    day_open = mc._fnum(cb.get("open"))
    day_high, day_low = mc._fnum(cb.get("high")), mc._fnum(cb.get("low"))
    today_vol = mc._fnum(cb.get("volume"))
    spread_pct = round((ask - bid) / ((ask + bid) / 2) * 100, 3) if (bid and ask and ask + bid) else None
    # intraday move vs prior close, and where in today's range we sit (1.0 = at the high = extended)
    intraday_pct = mc._fnum(cb.get("price_change_percent"))
    if intraday_pct is None and last and prev_close:
        intraday_pct = pct(last, prev_close)
    range_pos = (round((last - day_low) / (day_high - day_low), 3)
                 if (last is not None and day_high and day_low and day_high > day_low) else None)
    dollar_vol = round(today_vol * last, 0) if (today_vol and last) else None
    out.update(
        last=last,
        bid=bid, ask=ask, spread_pct=spread_pct,
        iv30=mc._fnum(cb.get("iv30")),
        gap_pct=pct(day_open, prev_close) if (day_open and prev_close) else None,
        intraday_pct=intraday_pct, range_pos=range_pos,
        today_volume=today_vol, dollar_volume_today=dollar_vol,
    )
    # Hard data problem only when we can't even confirm a live price/liquidity (Cboe down too).
    if last is None or spread_pct is None:
        out["error"] = "no_live_quote"
    out["data_note"] = (f"full history ({hist_src}) + intraday available" if closes
                        else "daily history unavailable (Cboe CDN + Yahoo both failed) — "
                             "trend/volume flags are null; judging on intraday + catalyst")

    # --- trend / momentum (history) ---
    if closes:
        c = closes
        out["ret_1d_pct"] = pct(c[-1], c[-2]) if len(c) >= 2 else None
        out["ret_5d_pct"] = pct(c[-1], c[-6]) if len(c) >= 6 else None
        out["ret_20d_pct"] = pct(c[-1], c[-21]) if len(c) >= 21 else None
        ma20 = round(statistics.fmean(c[-20:]), 2) if len(c) >= 20 else None
        ma50 = round(statistics.fmean(c[-50:]), 2) if len(c) >= 50 else None
        out["ma20"] = ma20
        out["ma50"] = ma50
        out["dist_ma20_pct"] = pct(c[-1], ma20) if ma20 else None
        out["dist_ma50_pct"] = pct(c[-1], ma50) if ma50 else None
        hi, lo = max(c), min(c)
        out["dist_3mo_high_pct"] = pct(c[-1], hi)
        out["dist_3mo_low_pct"] = pct(c[-1], lo)
        # 20-day annualized realized volatility
        if len(c) >= 21:
            rets = [(c[i] / c[i - 1] - 1) for i in range(len(c) - 20, len(c))]
            out["realized_vol_20d_annual_pct"] = round(statistics.pstdev(rets) * (252 ** 0.5) * 100, 1)
    # --- relative volume (PACE-adjusted: today's partial cumulative volume vs the same fraction of
    # a normal day, so a heavy-volume name reads as confirmed at midday, not only at the close) ---
    if vols:
        avg20 = round(statistics.fmean(vols[-20:]), 0) if len(vols) >= 20 else None
        frac = session_fraction()
        out["avg_volume_20d"] = avg20
        out["session_frac"] = round(frac, 2)
        out["rel_volume"] = (round(today_vol / (avg20 * frac), 2)
                             if (today_vol and avg20 and frac > 0) else None)

    # --- flags for the commit model ---
    # KEYLESS (always computed from Cboe — these are the primary gate now):
    #   spread_ok / liquid / iv_ok = tradeability; parabolic = chase guard.
    # HISTORY BONUS (null when history_ok is false — never false-on-missing-data):
    #   trend_up / volume_confirmed / extended / at_high enrich the call when daily data exists.
    d20 = out.get("dist_ma20_pct")
    d50 = out.get("dist_ma50_pct")
    rv = out.get("rel_volume")
    d3h = out.get("dist_3mo_high_pct")
    iv = out.get("iv30")
    out["flags"] = {
        # keyless tradeability / structure
        "spread_ok": spread_pct is not None and spread_pct < 0.5,
        "liquid": dollar_vol is not None and dollar_vol >= LIQ_FLOOR_USD,
        "iv_ok": iv is None or iv < IV_CEIL,           # unknown IV -> don't veto on it
        "parabolic": bool((out.get("gap_pct") is not None and out["gap_pct"] > PARABOLIC_GAP_PCT)
                          or (range_pos is not None and range_pos > 0.98
                              and (intraday_pct or 0) > PARABOLIC_MOVE_PCT)),
        # history bonus — null (unknown) when history is unavailable, so the model never reads a
        # data blackout as 'weak momentum'. Present => real confirmation signal.
        "trend_up": ((d20 > 0 and d50 > 0) if (closes and d20 is not None and d50 is not None) else None),
        "volume_confirmed": (rv >= 1.2 if (closes and rv is not None) else None),
        "extended": (d20 > 12 if (closes and d20 is not None) else None),
        "at_high": (d3h > -1.0 if (closes and d3h is not None) else None),
    }
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: dd_probe.py SYMBOL", file=sys.stderr)
        return 2
    sym = sys.argv[1].upper()
    data = probe(sym)
    TICK.mkdir(parents=True, exist_ok=True)
    (TICK / f"dd_{sym}.json").write_text(json.dumps(data, indent=2))
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
