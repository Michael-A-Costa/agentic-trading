#!/usr/bin/env python3
"""Offline 2x2 manage-DD eval: {Haiku, Sonnet} x {with WebFetch, without WebFetch}.

Runs the REAL run_manage_dd() against current holdings (from data/tick/context_latest.json)
under all four cells and prints the verdicts side by side. Production MANAGE_TOOLS is NOT
touched — the toolset is passed per-cell. The dd_probe is run ONCE per symbol and frozen
(skip_probe=True) so every arm sees identical quant data and only model/tools vary.

Usage:
    python3 scripts/manage_ab_eval.py                 # default: CAVA ASO TGTX
    python3 scripts/manage_ab_eval.py CAVA ELF        # specific holdings
    python3 scripts/manage_ab_eval.py --all           # every current holding

Cells run sequentially (the token/cost ledger is a shared global, so reset/summary per cell
needs serial execution for clean attribution). Wall-clock can be minutes per name.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from decide import (MANAGE_TOOLS, PYEXE, SCRIPTS, TICK, reset_usage,  # noqa: E402
                    run_manage_dd, usage_summary)

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"
WITH_FETCH = list(MANAGE_TOOLS)                                  # production set (has WebFetch)
NO_FETCH = [t for t in MANAGE_TOOLS if t != "WebFetch"]          # WebSearch + quote only

CELLS = [
    ("haiku  +fetch", HAIKU, WITH_FETCH),
    ("haiku  -fetch", HAIKU, NO_FETCH),
    ("sonnet +fetch", SONNET, WITH_FETCH),
    ("sonnet -fetch", SONNET, NO_FETCH),
]


def main() -> int:
    ctx = json.loads((TICK / "context_latest.json").read_text())
    caps = ctx["caps"]
    regime = ctx.get("regime", {})
    pm = {"cash": ctx["portfolio"]["cash"],
          "exposure": ctx["portfolio"].get("positions_value", 0.0)}
    posmap = {p["symbol"]: p for p in ctx.get("positions", [])}

    args = [a for a in sys.argv[1:] if a]
    if args == ["--all"]:
        syms = list(posmap)
    else:
        syms = args or ["CAVA", "ASO", "TGTX"]

    print(f"context: market_open={ctx.get('market_open')} data_stale={ctx.get('data_stale')} "
          f"regime={regime.get('posture')}/{regime.get('breadth_regime')}")
    print(f"holdings available: {', '.join(posmap)}")
    print(f"evaluating: {', '.join(syms)}\n")

    rows = []
    for sym in syms:
        p = posmap.get(sym)
        if not p:
            print(f"!! {sym} is not a current holding — skipping")
            continue
        # Freeze the quant snapshot ONCE so all four arms compare on identical probe data.
        try:
            subprocess.run([PYEXE, str(SCRIPTS / "dd_probe.py"), sym],
                           capture_output=True, text=True, timeout=60)
        except (subprocess.SubprocessError, OSError) as e:
            print(f"!! dd_probe {sym} failed ({e}) — arms will see no_dd")
        pr = p.get("risk") or {}
        print(f"=== {sym}  pnl%={p.get('pnl_pct')}  val=${p.get('value')}  "
              f"thesis={p.get('thesis_type')}  band={pr.get('band')} ===")
        for name, model, tools in CELLS:
            reset_usage()
            t0 = time.time()
            res = run_manage_dd(p, regime, caps, pm, model, tools=tools, skip_probe=True)
            el = time.time() - t0
            u = usage_summary()
            row = {"sym": sym, "cell": name.strip(), "model": model,
                   "webfetch": tools is WITH_FETCH,
                   "action": res.get("action"), "trim_fraction": res.get("trim_fraction"),
                   "conviction": res.get("conviction"), "hold_intent": res.get("hold_intent"),
                   "reason": res.get("reason", ""), "error": res.get("error"),
                   "secs": round(el, 1), "tokens": u["total_tokens"], "cost": u["cost_usd"]}
            rows.append(row)
            act = row["action"]
            tf = f" tf={row['trim_fraction']}" if act == "trim" else ""
            err = f" ERR={row['error']}" if row["error"] else ""
            print(f"  {name}: {act}{tf} conv={row['conviction']} "
                  f"| {row['secs']}s {row['tokens']:,}tok ${row['cost']:.4f}{err}")
            print(f"       reason: {(row['reason'] or '')[:240]}")
        print()

    out = TICK / "manage_ab_eval.json"
    out.write_text(json.dumps(rows, indent=2))

    # Compact summary table
    print("\n=== SUMMARY (action | secs | tokens | $) ===")
    print(f"{'sym':6} {'cell':14} {'action':6} {'secs':>6} {'tokens':>9} {'cost':>9}")
    for r in rows:
        print(f"{r['sym']:6} {r['cell']:14} {str(r['action']):6} {r['secs']:>6} "
              f"{r['tokens']:>9,} ${r['cost']:>8.4f}")

    # Agreement check: did with/without WebFetch diverge on action, per model?
    print("\n=== WebFetch divergence (does dropping WebFetch change the call?) ===")
    by = {(r["sym"], r["model"], r["webfetch"]): r for r in rows}
    for sym in dict.fromkeys(r["sym"] for r in rows):
        for model, tag in ((HAIKU, "haiku"), (SONNET, "sonnet")):
            a = by.get((sym, model, True))
            b = by.get((sym, model, False))
            if a and b:
                same = a["action"] == b["action"]
                dtok = (a["tokens"] or 0) - (b["tokens"] or 0)
                flag = "SAME action" if same else f"DIVERGED {a['action']}->{b['action']}"
                print(f"  {sym:6} {tag:7}: {flag:24} | webfetch saved/cost {dtok:+,} tok, "
                      f"{a['secs'] - b['secs']:+.0f}s")

    print(f"\nrows written -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
