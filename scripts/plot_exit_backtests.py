#!/usr/bin/env python3
"""
plot_exit_backtests.py — render the exit-policy backtest tables (playbook §6a-6d) as charts.

Outputs to data/backtest/plots/:
  1. exit_policies_mean_median.png   — mean vs median per policy, LARGE & MIDCAP (deduped PEAD entry)
  2. harness_fix_old_vs_new.png      — the 2026-06-10 AM numbers (overlapping entries) vs the fixed
                                       deduped harness: how much the fix moved each policy
  3. disco_cohort_portfolio.png      — PEAD-cohort vs DISCO movers-cohort frontier + the 6-slot
                                       portfolio compounding on the movers stream (the TP10 flip)
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_gap_drift as gd
import backtest_exit_policy as bx
import backtest_quickwin as qw
from backtest_sweeps import find_mover_entries

OUT = Path(__file__).resolve().parent.parent / "data" / "backtest" / "plots"
OUT.mkdir(parents=True, exist_ok=True)

GAP, VOL, HOLD, COST = 7.0, 2.0, 15, 15.0
BASE = {"stop": 12, "softcut": 8, "be": 12, "cost_bps": COST}


def P(**kw):
    d = dict(BASE)
    d.update(kw)
    return d


POLICIES = [
    ("LET-RUN\n(tp40 tr15@20)",  P(tp=40, trail=15, activate=20)),
    ("tight TP8",                P(tp=8)),
    ("tight TP10",               P(tp=10)),
    ("tight TP15",               P(tp=15)),
    ("scale 50%@5\n+ run",       P(tp=40, trail=15, activate=20, tiers=[(5, 0.5)])),
    ("scale 33@5,33@8\n+ run",   P(tp=40, trail=15, activate=20, tiers=[(5, 0.33), (8, 0.33)])),
    ("scale 50%@8\n+ tr12@12",   P(tp=40, trail=12, activate=12, tiers=[(8, 0.5)])),
]

UNIS = [("LARGE (pead proxy, trustworthy)", gd.LARGE), ("MIDCAP (disco proxy, BIASED)", gd.MIDCAP)]


def metrics(entries, pol):
    return qw.evalpol(entries, HOLD, pol)


def bar_pair(ax, names, means, medians, title, n):
    x = range(len(names))
    w = 0.38
    bm = ax.bar([i - w / 2 for i in x], means, w, label="mean", color="#4878a8")
    bd = ax.bar([i + w / 2 for i in x], medians, w, label="median", color="#e8923a")
    ax.axhline(0, color="k", lw=0.8)
    ax.set_xticks(list(x))
    ax.set_xticklabels(names, fontsize=7.5)
    ax.set_ylabel("net % per trade")
    ax.set_title(f"{title}  ({n} entries, hold {HOLD}d, {COST:.0f}bps/leg)", fontsize=10)
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)
    for b in list(bm) + list(bd):
        ax.annotate(f"{b.get_height():+.2f}", (b.get_x() + b.get_width() / 2, b.get_height()),
                    ha="center", va="bottom" if b.get_height() >= 0 else "top", fontsize=6.5)


def chart1():
    fig, axes = plt.subplots(2, 1, figsize=(11, 8))
    for ax, (uname, u) in zip(axes, UNIS):
        entries = qw.dedupe_entries(bx.find_entries(u, GAP, VOL, HOLD, False), HOLD)
        rows = [metrics(entries, pol) for _, pol in POLICIES]
        bar_pair(ax, [n for n, _ in POLICIES], [r["mean"] * 100 for r in rows],
                 [r["median"] * 100 for r in rows], uname, len(entries))
        for i, r in enumerate(rows):   # annotate win% / gaveback under the axis
            ax.annotate(f"win {r['win']*100:.0f}%\ngb {r['gaveback']*100:.0f}%",
                        (i, ax.get_ylim()[0]), ha="center", va="bottom", fontsize=6.5, color="#666")
    fig.suptitle("Exit policies — mean vs median per trade (deduped PEAD-entry cohort, 2026-06-10 PM)",
                 fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT / "exit_policies_mean_median.png", dpi=150)
    plt.close(fig)


def chart2():
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    for col, (uname, u) in enumerate(UNIS):
        raw = bx.find_entries(u, GAP, VOL, HOLD, False)
        ded = qw.dedupe_entries(raw, HOLD)
        old = [metrics(raw, pol) for _, pol in POLICIES]
        new = [metrics(ded, pol) for _, pol in POLICIES]
        for row, key in enumerate(("mean", "median")):
            ax = axes[row][col]
            x = range(len(POLICIES))
            w = 0.38
            ax.bar([i - w / 2 for i in x], [r[key] * 100 for r in old], w,
                   label=f"prev convo (overlapping, n={len(raw)})", color="#b0b0b0")
            ax.bar([i + w / 2 for i in x], [r[key] * 100 for r in new], w,
                   label=f"fixed harness (deduped, n={len(ded)})", color="#4878a8")
            ax.axhline(0, color="k", lw=0.8)
            ax.set_xticks(list(x))
            ax.set_xticklabels([n for n, _ in POLICIES], fontsize=6.5)
            ax.set_title(f"{uname} — {key}", fontsize=10)
            ax.set_ylabel(f"net {key} %")
            ax.grid(axis="y", alpha=0.3)
            if row == 0 and col == 0:
                ax.legend(fontsize=7.5)
    fig.suptitle("Harness fix: previous-convo numbers (overlapping entries) vs deduped — direction holds",
                 fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT / "harness_fix_old_vs_new.png", dpi=150)
    plt.close(fig)


def chart3():
    fig, (axl, axr) = plt.subplots(1, 2, figsize=(13, 5.5))
    # Left: mean-vs-median frontier, PEAD cohort vs DISCO movers cohort (MIDCAP)
    u = gd.MIDCAP
    pead_e = qw.dedupe_entries(bx.find_entries(u, GAP, VOL, HOLD, False), HOLD)
    mov_e = qw.dedupe_entries(find_mover_entries(u, GAP, HOLD, "all"), HOLD)
    marks = ["o", "s", "^", "v", "D", "P", "X"]
    for ents, color, label in ((pead_e, "#4878a8", f"PEAD entry (n={len(pead_e)})"),
                               (mov_e, "#c0504d", f"DISCO movers entry (n={len(mov_e)})")):
        for (name, pol), m in zip(POLICIES, marks):
            r = metrics(ents, pol)
            axl.scatter(r["mean"] * 100, r["median"] * 100, c=color, marker=m, s=70,
                        label=None, edgecolors="k", linewidths=0.4, zorder=3)
            axl.annotate(name.replace("\n", " "), (r["mean"] * 100, r["median"] * 100),
                         fontsize=6, xytext=(4, 3), textcoords="offset points")
        axl.scatter([], [], c=color, marker="o", label=label)
    axl.axhline(0, color="k", lw=0.8)
    axl.axvline(0, color="k", lw=0.8)
    axl.set_xlabel("mean net % / trade")
    axl.set_ylabel("median net % / trade")
    axl.set_title("MIDCAP: owner-goal frontier — PEAD cohort vs the entry disco actually trades",
                  fontsize=10)
    axl.legend(fontsize=8)
    axl.grid(alpha=0.3)

    # Right: 6-slot portfolio compounding on the movers stream (the ranking flip)
    K = 6
    names, finals = [], []
    for name, pol in POLICIES:
        sims = []
        for _, bars, i, entry in mov_e:
            r, d, _ = qw.simulate(bars, i, entry, HOLD, pol)
            sims.append((bars[i]["date"], bars[i + d]["date"], r))
        sims.sort()
        equity, busy = 1.0, []
        for ed, xd, r in sims:
            still = []
            for bxd, alloc, br in busy:
                if bxd <= ed:
                    equity += alloc * br
                else:
                    still.append((bxd, alloc, br))
            busy = still
            if len(busy) < K:
                busy.append((xd, equity / K, r))
        for _, alloc, br in busy:
            equity += alloc * br
        names.append(name)
        finals.append(equity)
    colors = ["#c0504d" if f == max(finals) else "#8aa9c9" for f in finals]
    bars_ = axr.bar(range(len(names)), finals, color=colors)
    axr.set_xticks(range(len(names)))
    axr.set_xticklabels(names, fontsize=6.5)
    axr.set_ylabel("terminal equity (×, start = 1)")
    axr.set_title(f"MIDCAP movers stream, {K} slots: capital-constrained compounding\n"
                  "(single path — read big gaps only; survivorship inflates magnitude)", fontsize=10)
    axr.grid(axis="y", alpha=0.3)
    for b, f in zip(bars_, finals):
        axr.annotate(f"{f:.2f}x", (b.get_x() + b.get_width() / 2, f), ha="center",
                     va="bottom", fontsize=7.5)
    fig.tight_layout()
    fig.savefig(OUT / "disco_cohort_portfolio.png", dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    chart1()
    print("wrote", OUT / "exit_policies_mean_median.png")
    chart2()
    print("wrote", OUT / "harness_fix_old_vs_new.png")
    chart3()
    print("wrote", OUT / "disco_cohort_portfolio.png")
