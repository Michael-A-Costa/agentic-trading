#!/usr/bin/env python3
"""
backtest_megasweep.py — the FULL FACTORIAL exit-policy search (playbook §6e).

Every combination of the exit schedule's axes:
  stop x softcut x breakeven x take-profit x trail(width@activate) x scale-out tiers
~52k valid configs after pruning (tp must clear the highest tier), each evaluated on three cohorts:
  PEAD-L  = gap7/vol2 on LARGE   (the pead book's world)
  PEAD-M  = gap7/vol2 on MIDCAP  (disco when it catches a qualified gap)
  MOV-M   = movers >=7% c/c on MIDCAP (the entry disco actually trades)

Anti-overfit guard: per-trade returns are split by ENTRY-YEAR PARITY (even/odd years = half A/B).
Leaderboards rank on half A and show half B alongside — a config that only wins on one half is
noise. MOV-M also gets a 6-slot portfolio terminal-equity per config.

Results: data/backtest/megasweep_results.json + printed leaderboards.
Run:  python3 scripts/backtest_megasweep.py            (multiprocess, ~minutes)
      python3 scripts/backtest_megasweep.py --workers 1
"""
from __future__ import annotations

import argparse
import itertools
import json
import multiprocessing as mp
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_gap_drift as gd
import backtest_exit_policy as bx
import backtest_quickwin as qw
from backtest_sweeps import find_mover_entries

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "backtest" / "megasweep_results.json"

GAP, VOL, HOLD, COST, SLOTS = 7.0, 2.0, 15, 15.0, 6

STOPS = [8, 10, 12, 16, None]
SOFTCUTS = [6, 8, 10, None]
BES = [None, 10, 12, 15]
TPS = [6, 8, 10, 12, 15, 20, 25, 30, 40, None]
TRAILS = [  # (trail%, activate%); None = off
    None, (8, 8), (10, 10), (12, 12), (15, 12), (15, 20), (20, 20), (10, 0), (15, 0),
]
TIERS = [
    None,
    [(3, 0.5)], [(5, 0.5)], [(6, 0.5)], [(8, 0.5)], [(10, 0.5)], [(12, 0.5)],
    [(5, 0.25)], [(8, 0.25)],
    [(5, 0.33), (10, 0.33)], [(6, 0.33), (10, 0.33)], [(4, 0.33), (8, 0.33)],
    [(8, 0.33), (12, 0.33)],
    [(5, 0.66)], [(8, 0.66)], [(5, 0.75)], [(5, 0.5), (10, 0.25)],
]


def grid():
    for stop, sc, be, tp, tr, tiers in itertools.product(STOPS, SOFTCUTS, BES, TPS, TRAILS, TIERS):
        if tp is not None and tiers and tp <= max(g for g, _ in tiers):
            continue  # TP below/at a tier never lets the tier matter — degenerate duplicate
        pol = {"stop": stop, "softcut": sc, "be": be, "tp": tp, "cost_bps": COST}
        if tr:
            pol["trail"], pol["activate"] = tr
        if tiers:
            pol["tiers"] = tiers
        yield pol


def pol_label(p):
    bits = [f"stop{p['stop'] or 'X'}", f"sc{p['softcut'] or 'X'}", f"be{p['be'] or 'X'}",
            f"tp{p['tp'] or 'X'}"]
    bits.append(f"tr{p['trail']}@{p['activate']}" if p.get("trail") else "trX")
    bits.append("+".join(f"{int(f*100)}%@{g}" for g, f in p["tiers"]) if p.get("tiers") else "tierX")
    return " ".join(bits)


# ---- worker globals (built once per process) ----
_COHORTS = None


def _init():
    global _COHORTS
    pead_L = qw.dedupe_entries(bx.find_entries(gd.LARGE, GAP, VOL, HOLD, False), HOLD)
    pead_M = qw.dedupe_entries(bx.find_entries(gd.MIDCAP, GAP, VOL, HOLD, False), HOLD)
    mov_M = qw.dedupe_entries(find_mover_entries(gd.MIDCAP, GAP, HOLD, "all"), HOLD)
    _COHORTS = {"PEAD-L": pead_L, "PEAD-M": pead_M, "MOV-M": mov_M}


def _metrics(rets):
    n = len(rets)
    if not n:
        return {}
    m, sd = gd.stats(rets)
    return {"mean": m, "median": gd.median(rets), "win": sum(1 for x in rets if x > 0) / n,
            "sharpe": (m / sd) if sd else 0.0, "n": n}


def eval_one(pol):
    """All cohorts, one pass per cohort: full/half-A/half-B metrics (+ portfolio x on MOV-M)."""
    out = {"pol": pol, "label": pol_label(pol)}
    for cname, entries in _COHORTS.items():
        rets, sims, peaks = [], [], []
        for _, bars, i, entry in entries:
            r, d, pk = qw.simulate(bars, i, entry, HOLD, pol)
            rets.append(r)
            peaks.append(pk)
            sims.append((bars[i]["date"], bars[i + d]["date"], r))
        full = _metrics(rets)
        full["gaveback"] = sum(1 for r, pk in zip(rets, peaks) if pk >= 5.0 and r < 0.02) / len(rets)
        a = _metrics([r for (ed, _, r) in sims if int(ed[:4]) % 2 == 0])
        b = _metrics([r for (ed, _, r) in sims if int(ed[:4]) % 2 == 1])
        row = {"full": full, "A": a, "B": b}
        if cname == "MOV-M":  # 6-slot portfolio terminal equity
            sims.sort()
            equity, busy = 1.0, []
            for ed, xd, r in sims:
                busy2 = []
                for bxd, alloc, br in busy:
                    if bxd <= ed:
                        equity += alloc * br
                    else:
                        busy2.append((bxd, alloc, br))
                busy = busy2
                if len(busy) < SLOTS:
                    busy.append((xd, equity / SLOTS, r))
            for _, alloc, br in busy:
                equity += alloc * br
            row["port_x"] = equity
        out[cname] = row
    return out


def lead(rows, cohort, key, title, k=12, where=None, half="full"):
    pool = [r for r in rows if (where is None or where(r))]
    pool.sort(key=lambda r: r[cohort][half].get(key, 0), reverse=True)
    print(f"\n  TOP {k} — {title}")
    print(f"  {'config':<58}{'mean':>7}{'med':>7}{'win':>5}{'shrp':>7}{'gb':>4} | {'meanA':>7}{'meanB':>7}"
          + ("   port_x" if cohort == "MOV-M" else ""))
    for r in pool[:k]:
        f, a, b = r[cohort]["full"], r[cohort]["A"], r[cohort]["B"]
        line = (f"  {r['label']:<58}{f['mean']*100:>+6.2f}%{f['median']*100:>+6.2f}%{f['win']*100:>4.0f}%"
                f"{f['sharpe']:>7.3f}{f['gaveback']*100:>3.0f}% | {a.get('mean',0)*100:>+6.2f}%"
                f"{b.get('mean',0)*100:>+6.2f}%")
        if cohort == "MOV-M":
            line += f"{r[cohort]['port_x']:>8.2f}x"
        print(line)
    return pool[:k]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=max(1, mp.cpu_count() - 2))
    args = ap.parse_args()

    cfgs = list(grid())
    print(f"factorial grid: {len(cfgs)} valid configs x 3 cohorts "
          f"(PEAD-L 344, PEAD-M 522, MOV-M 1840 entries) | {args.workers} workers", flush=True)
    t0 = time.time()
    if args.workers > 1:
        ctx = mp.get_context("fork")
        with ctx.Pool(args.workers, initializer=_init) as pool:
            rows = []
            for i, r in enumerate(pool.imap_unordered(eval_one, cfgs, chunksize=64)):
                rows.append(r)
                if (i + 1) % 5000 == 0:
                    print(f"  {i+1}/{len(cfgs)} ({time.time()-t0:.0f}s)", flush=True)
    else:
        _init()
        rows = [eval_one(p) for p in cfgs]
    print(f"done in {time.time()-t0:.0f}s", flush=True)

    OUT.write_text(json.dumps(rows))
    print(f"all rows -> {OUT}")

    # ---- reference rows ----
    def find(label_sub):
        for r in rows:
            if r["label"] == label_sub:
                return r
    letrun = find("stop12 sc8 be12 tp40 tr15@20 tierX")
    print("\nREFERENCE (current live let-run, stop12/sc8/be12/tp40/tr15@20):")
    for c in ("PEAD-L", "PEAD-M", "MOV-M"):
        f = letrun[c]["full"]
        extra = f"  port {letrun[c]['port_x']:.2f}x" if c == "MOV-M" else ""
        print(f"  {c}: mean {f['mean']*100:+.2f}% med {f['median']*100:+.2f}% win {f['win']*100:.0f}%"
              f" sharpe {f['sharpe']:.3f} gb {f['gaveback']*100:.0f}%{extra}")

    print("\n" + "=" * 110)
    print("PEAD BOOK (PEAD-L cohort) — ranked on half A (even entry-years), half B shown as the check")
    print("=" * 110)
    lead(rows, "PEAD-L", "mean", "by mean (half A) — total-return objective", half="A")
    lead(rows, "PEAD-L", "sharpe", "by sharpe (half A) — risk-adjusted", half="A")

    print("\n" + "=" * 110)
    print("DISCO BOOK (MOV-M cohort) — the entry it actually trades")
    print("=" * 110)
    lr_mean = letrun["MOV-M"]["full"]["mean"]
    lead(rows, "MOV-M", "sharpe", "by sharpe (half A)", half="A")
    lead(rows, "MOV-M", "median", f"by median (half A), mean >= let-run ({lr_mean*100:+.2f}%)",
         where=lambda r: r["MOV-M"]["full"]["mean"] >= lr_mean, half="A")
    rows_port = sorted(rows, key=lambda r: r["MOV-M"]["port_x"], reverse=True)
    print("\n  TOP 12 — by 6-slot PORTFOLIO terminal equity (single path; magnitude inflated, read shape)")
    for r in rows_port[:12]:
        f = r["MOV-M"]["full"]
        print(f"  {r['label']:<58}{f['mean']*100:>+6.2f}%{f['median']*100:>+6.2f}%{f['win']*100:>4.0f}%"
              f"{f['sharpe']:>7.3f}{f['gaveback']*100:>3.0f}%{r['MOV-M']['port_x']:>8.2f}x")

    # split-half stability of the whole grid (is ANY of this real?)
    import math
    def spearman(xs, ys):
        def rank(v):
            order = sorted(range(len(v)), key=lambda i: v[i])
            rk = [0.0] * len(v)
            for pos, i in enumerate(order):
                rk[i] = pos
            return rk
        rx, ry = rank(xs), rank(ys)
        mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
        num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
        den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
        return num / den if den else 0.0
    for c in ("PEAD-L", "MOV-M"):
        xs = [r[c]["A"].get("mean", 0) for r in rows]
        ys = [r[c]["B"].get("mean", 0) for r in rows]
        print(f"\nSplit-half rank correlation of config mean, {c}: spearman={spearman(xs, ys):+.3f} "
              f"(near 0 = leaderboard order is noise; high = structure is real)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
