#!/usr/bin/env python3
"""
dd_probe.py — deep, script-side due-diligence on ONE candidate before committing capital.

Runs only in Stage 2 (when the cheap screen flags a real entry candidate), so cost is bounded.
Gathers quantitative DD from public sources — NO LLM tokens spent here — and writes a compact
JSON the Stage-2 commit model (Opus, with web news search) then judges.

Signals:
  - multi-timeframe trend: 1/5/20-day returns, vs 20/50-day MA, distance from 3-mo high/low
  - relative volume (today vs 20-day average)  — is the move backed by volume?
  - volatility: 20-day annualized realized vol, plus Cboe IV30
  - liquidity/cost: bid/ask spread %  — the edge must clear the spread
  - gap %, range position
  - convenience flags: trend_up, volume_confirmed, extended, spread_ok

Sources: Yahoo daily history (3mo) for trend/vol/volume; Cboe live quote for bid/ask/IV30/gap.
Both keyless. Degrades gracefully (nulls) if a source is unavailable.

Usage:  python3 scripts/dd_probe.py META            # prints JSON, also writes data/tick/dd_META.json
"""
from __future__ import annotations

import json
import statistics
import sys
import urllib.parse
from pathlib import Path

import market_conditions as mc  # sibling: _http_get, _fnum, CBOE_URL, ET

REPO = Path(__file__).resolve().parent.parent
TICK = REPO / "data" / "tick"
YH = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=3mo&interval=1d"


def pct(a: float, b: float) -> float | None:
    return round((a / b - 1) * 100, 2) if (a is not None and b) else None


def yahoo_history(sym: str) -> dict:
    """Daily closes/volumes for the last ~3 months from Yahoo (keyless). {} on failure."""
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


def cboe_quote(sym: str) -> dict:
    try:
        d = json.loads(mc._http_get(mc.CBOE_URL.format(sym=urllib.parse.quote(sym.upper()))))
        return d.get("data") or {}
    except (OSError, ValueError, KeyError):
        return {}


def probe(sym: str) -> dict:
    sym = sym.upper()
    hist = yahoo_history(sym)
    cb = cboe_quote(sym)
    closes = hist.get("closes") or []
    vols = hist.get("volumes") or []

    out: dict = {"symbol": sym, "sources": {"yahoo_history": bool(closes), "cboe": bool(cb)}}

    # --- live quote / liquidity / gap (Cboe) ---
    last = mc._fnum(cb.get("current_price")) or (closes[-1] if closes else None)
    bid, ask = mc._fnum(cb.get("bid")), mc._fnum(cb.get("ask"))
    prev_close = mc._fnum(cb.get("prev_day_close"))
    day_open = mc._fnum(cb.get("open"))
    spread_pct = round((ask - bid) / ((ask + bid) / 2) * 100, 3) if (bid and ask and ask + bid) else None
    out.update(
        last=last,
        bid=bid, ask=ask, spread_pct=spread_pct,
        iv30=mc._fnum(cb.get("iv30")),
        gap_pct=pct(day_open, prev_close) if (day_open and prev_close) else None,
    )

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
    # --- relative volume ---
    if vols:
        today_vol = mc._fnum(cb.get("volume")) or (vols[-1] if vols else None)
        avg20 = round(statistics.fmean(vols[-20:]), 0) if len(vols) >= 20 else None
        out["volume"] = today_vol
        out["avg_volume_20d"] = avg20
        out["rel_volume"] = round(today_vol / avg20, 2) if (today_vol and avg20) else None

    # --- convenience flags for the commit model (None-safe) ---
    d20 = out.get("dist_ma20_pct")
    d50 = out.get("dist_ma50_pct")
    rv = out.get("rel_volume")
    d3h = out.get("dist_3mo_high_pct")
    out["flags"] = {
        "trend_up": (d20 is not None and d20 > 0) and (d50 is not None and d50 > 0),
        "volume_confirmed": rv is not None and rv >= 1.2,
        "extended": (d3h is not None and d3h > -1.0) or (d20 is not None and d20 > 8),
        "spread_ok": spread_pct is not None and spread_pct < 0.5,
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
