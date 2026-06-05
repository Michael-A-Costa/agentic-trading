#!/usr/bin/env python3
"""
catalyst_log.py — forward filter-lift ledger (the leakage-free pump-filter test).

Records EVERY gap candidate the Stage-2 agent evaluated (commit OR reject), so we can later measure —
FORWARD and leakage-free — whether the agent's "real catalyst vs pump" call actually predicts the
multi-day drift. At decision time we know only the event + the verdict; the realized N-day drift is
filled in later by scripts/catalyst_filter_report.py from keyless daily history.

Why forward and not a historical backtest: an LLM scoring a PAST gap already knows how it resolved
(post-event news + training-cutoff memorization), so a historical filter backtest manufactures a
phantom lift (cf. the "Profit Mirage" knowledge-cutoff decay in research/ai-trading-landscape-2026H1.md).
Scoring each event LIVE, at the gap, with no hindsight — which the paper engine already does — is the
only honest measurement. This module just captures it.

One row per (symbol, eval_date), append-only JSONL at data/catalyst_events.jsonl. Best-effort: a
logging miss must never break a tick.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "data" / "catalyst_events.jsonl"


def _existing_keys() -> set:
    """(symbol, eval_date) pairs already logged, so the same gap event isn't double-counted across
    ticks (the DD cache re-serves a verdict for the same name within a day)."""
    keys: set = set()
    if LEDGER.exists():
        try:
            for line in LEDGER.read_text().splitlines():
                if line.strip():
                    r = json.loads(line)
                    keys.add((r.get("symbol"), r.get("eval_date")))
        except (OSError, ValueError):
            pass
    return keys


def record_events(context: dict, candidates: list, dd_results: list) -> int:
    """Append one event per evaluated gap candidate (dedup by symbol+eval_date). Returns rows written.

    context     : the tick context (for ts + the candidate quotes / ref price)
    candidates  : screen entry_candidates (carry gap_pct / vol_mult)
    dd_results  : Stage-2 verdicts (carry decision / catalyst_type / conviction)
    """
    try:
        eval_date = (context.get("ts_et") or "")[:10]
        if not eval_date:
            return 0
        cand_by_sym = {c.get("symbol"): c for c in (candidates or [])}             # gap/vol from the screen
        last_by_sym = {c.get("symbol"): c.get("last") for c in context.get("candidates", [])}  # ref price
        seen = _existing_keys()
        rows = []
        for d in (dd_results or []):
            sym = d.get("symbol")
            dec = d.get("decision")
            if not sym or dec not in ("commit", "reject"):     # never log 'error' (not a real verdict)
                continue
            if (sym, eval_date) in seen:
                continue
            seen.add((sym, eval_date))
            sc = cand_by_sym.get(sym, {})
            ctype = d.get("catalyst_type")
            rows.append({
                "ts_utc": context.get("ts_utc"),
                "eval_date": eval_date,
                "symbol": sym,
                "gap_pct": sc.get("gap_pct"),
                "vol_mult": sc.get("vol_mult"),
                "ref_price": last_by_sym.get(sym),              # entry basis: the quote at evaluation
                "agent_decision": dec,
                "is_real": dec == "commit",                     # agent says: a real, tradeable catalyst
                "is_pump": dec == "reject" and (ctype in (None, "none")),  # rejected with no catalyst = pump
                "catalyst_type": ctype,
                "conviction": d.get("conviction"),
                "reason": (d.get("reason") or "")[:200],
            })
        if rows:
            LEDGER.parent.mkdir(parents=True, exist_ok=True)
            with LEDGER.open("a") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
        return len(rows)
    except Exception as e:                                       # best-effort: never sink a tick
        sys.stderr.write(f"[catalyst_log] record failed (ignored): {e}\n")
        return 0
