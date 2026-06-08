#!/usr/bin/env python3
"""
hold_risk.py — deterministic risk score for an open position (Tier-1 holding monitor).

Cheap, per-tick, NO LLM. Ranks holdings by risk from data we already have — live price vs the
entry/stop, plus the ORIGINAL DD (conviction + hold intent) — so the engine can:
  (1) protective-SELL a genuinely deteriorating loser "in the meantime" (a SMART soft-cut, tighter
      than the dumb hard stop, but gated so it doesn't whipsaw a noisy-but-fine position), and
  (2) decide how soon each holding's next (expensive) agent re-DD is due — riskier = sooner (Tier 2).

The hard STOP_LOSS_PCT stop stays as the catastrophe backstop UNDER this. This module decides
nothing on missing data (fail-safe: unknown -> low risk, no sell).
"""
from __future__ import annotations

from datetime import datetime

DEFAULT_REDD_TTL = {"low": 60.0, "medium": 20.0, "high": 5.0, "critical": 0.0}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def score(pos: dict, now_utc: datetime, soft_cut_pct: float = 4.0,
          redd_ttl: dict | None = None) -> dict:
    """Score one position. `pos` carries entry_price, stop_price, last, pnl_pct, conviction,
    hold_intent, entry_ts, range_pos (0=day low,1=day high), intraday_pct.

    Returns {risk:0-100, band, reasons:[], protective_sell:bool, sell_reason, redd_ttl_min}.
    """
    redd_ttl = redd_ttl or DEFAULT_REDD_TTL
    entry, stop, last = _f(pos.get("entry_price")), _f(pos.get("stop_price")), _f(pos.get("last"))
    pnl = _f(pos.get("pnl_pct"))
    conv = (pos.get("conviction") or "").lower()
    intent = (pos.get("hold_intent") or "").lower()
    rng = _f(pos.get("range_pos"))         # 0 = at the day's low, 1 = at the high
    intr = _f(pos.get("intraday_pct"))
    reasons: list[str] = []
    risk = 0.0

    # 1) proximity to the hard stop — the dominant term. fraction of the way down to the stop:
    #    1.0 = at/above the reference high, 0.0 = at the stop. Closer to the stop = hotter.
    #    Only meaningful while the stop sits BELOW entry (a position at risk of a losing stop-out).
    #    Once a TRAILING stop has ratcheted to/above entry the trade is in profit and the resting/
    #    trailing stop owns the downside — this term would otherwise divide by a negative (entry-stop)
    #    and spuriously flag a green winner CRITICAL, so we skip it there. ref_high=max(entry,last)
    #    also avoids penalising an in-profit position against its own high. (Losers: ref_high==entry,
    #    so the score is identical to the original (last-stop)/(entry-stop).)
    prox = None
    if entry is not None and stop is not None and last is not None and entry > stop:
        ref_high = max(entry, last)
        prox = max(0.0, min(1.0, (last - stop) / (ref_high - stop)))
        risk += (1 - prox) * 40.0
        if prox < 0.5:
            reasons.append(f"{int((1 - prox) * 100)}% of the way to stop")

    # 2) unrealized loss (each 1% down adds 2, capped at 20)
    if pnl is not None and pnl < 0:
        risk += min(abs(pnl), 10.0) * 2.0
        reasons.append(f"down {pnl}%")

    # 3) adverse intraday momentum: falling and/or pinned near the day's low
    if intr is not None and intr < 0:
        risk += min(abs(intr), 8.0) * 1.5
    if rng is not None and rng < 0.25:
        risk += 12.0
        reasons.append("near intraday low")

    # 4) conviction of the original thesis (a low-conviction bet is riskier to hold)
    risk += {"low": 12.0, "medium": 6.0, "high": 0.0}.get(conv, 8.0)

    # 5) hold-intent vs age — a 'scalp' still open hours later is a stale thesis
    age_h = None
    if pos.get("entry_ts"):
        try:
            age_h = (now_utc - datetime.fromisoformat(pos["entry_ts"])).total_seconds() / 3600.0
        except (ValueError, TypeError):
            age_h = None
    if intent == "scalp" and age_h is not None and age_h > 2:
        risk += 12.0
        reasons.append(f"scalp held {age_h:.1f}h")

    risk = max(0.0, min(100.0, risk))
    band = ("critical" if risk >= 70 else "high" if risk >= 45
            else "medium" if risk >= 25 else "low")

    # Protective sell: a genuinely deteriorating LOSER (down past the soft-cut AND falling), but NOT a
    # high-conviction runner — or anything that's gone critical. A position that merely dipped and
    # stabilized (not falling) is left alone, so this doesn't whipsaw like a flat tight stop would.
    falling = (intr is not None and intr < 0) or (rng is not None and rng < 0.30)
    deep = pnl is not None and pnl <= -soft_cut_pct
    runner = conv == "high" and intent == "runner"
    protective_sell = bool((deep and falling and not runner) or band == "critical")
    sell_reason = ""
    if protective_sell:
        head = "risk CRITICAL" if band == "critical" else f"soft-cut {pnl}% (<= -{soft_cut_pct}%) & falling"
        sell_reason = f"{head} [risk {int(risk)}]: {', '.join(reasons[:3])}"

    return {"risk": round(risk, 1), "band": band, "reasons": reasons,
            "protective_sell": protective_sell, "sell_reason": sell_reason,
            "redd_ttl_min": redd_ttl.get(band, 20.0)}
