# Exit-Strategy Findings & Recommendations — 2026-06-10

**What this is:** the conclusions of the full exit-policy research campaign (playbook §6a–6e):
~62 hand-curated strategies across ~900 cohort evaluations, then a **101,520-config full-factorial
search** (~557k evaluations: 3 cohorts × split-half validation × a 6-slot portfolio sim each).
Methodology, raw tables, and reproduction commands live in `strategies/exit-backtest-playbook.md`;
charts in `data/backtest/plots/`; the full grid in `data/backtest/megasweep_results.json`.

**The question:** the account runs two books — `pead` (validated mega-cap gap-drift edge) and
`disco` (free-rein discretionary movers). What exit schedule should each run, and how should the
owner think about the mean-vs-median dial?

---

## 1. The five findings that matter

### F1. The two books need OPPOSITE exits — and now we know why.
The exit that's right depends on the **entry's return shape**, not on taste:
- **PEAD entries** (gap ≥7% + 2× volume, mega-cap) have a fat right tail — a minority of trades
  drift +20–40% and pay for everything. Any harvest (TP or scale-out) clips that tail: every
  quick-win variant is *significantly* mean-worse (paired bootstrap CIs exclude zero). Let-run wins.
- **Movers entries** (what disco actually buys: big day's gainers, no gap/volume gate) have a
  **fat middle and no reliable right tail** — the median trade *touches* +10% within 15 days, but
  let-run rides it back down (win% 38%, median −0.30%, give-back 34%). Here harvesting is free on
  mean (CIs straddle zero) and transformative on everything else.

### F2. Under binding capital, full-exit TPs dominate EVERYTHING — including scale-outs.
The per-trade tables hide the account-level effect: live disco sees ~25 candidates/day against ~6
full-size slots, so capital binds. In the 6-slot portfolio sim on the movers stream:
- tight TP frees the slot in ~5–6 days and recycles: protected TP15 compounds **13.1x**, TP10
  **7.0x**, vs let-run **2.3x** (and with lower drawdown).
- **scale-outs look pretty per-trade but lose at the account level (~1.9–2.7x)** — the remnant
  occupies the slot for the full hold, so you pay let-run's slot cost for half the position.
This flipped my earlier recommendation: the 50%@8 trim was the right call *per-trade*; the full
TP is the right call *for the account*.

**Slot-count robustness (owner challenge, tested K = 4→30, playbook §6f):** the TP-over-let-run
ranking holds at *every* slot count on every movers stream — the margin narrows as utilization
approaches 100% (combined stream, K=30: TP15 1.73x vs let-run 1.48x) but never flips, because even
with idle capital TP15 earns the same per-trade mean in 6.7 days that let-run takes 8.9 to earn.
Note the mapping: the sim's K = position-sized chunks of capital, and live's 15%-position /
95%-exposure caps make the effective K ≈ 6 full-size (≈10–15 with conviction-tiered smaller
entries) even when 20–30 names are held — `MAX_OPEN_POSITIONS=30` caps the name count, not the
sizing. In that effective band the TP advantage is 2–4x, not marginal. Scale-outs and ladders never
beat the TP family at any K.

### F3. Protection (stop12 / softcut8) is an insurance premium of ~1%/trade — pay it anyway.
The factorial's raw "winners" on the disco cohort are all **no-stop configs** (mean roughly doubles
without the stop). That is the survivorship trap, quantified: our universe only contains names that
still exist — the −60% delisted disasters that the stop exists for are invisible to the backtest.
Priced on matched configs (tp12): stop12/sc8 costs mean +1.88% → +0.91%. We pay it. Also: stop8 is
strictly worse than stop12 everywhere (clips normal noise), and the breakeven rung (be10–15) is
free. **Protection layer stays exactly as live: stop12 / softcut8 / be12.**

### F4. The pead config is already at the global optimum — with one optional tweak.
Out of 101,520 configs, the top of the PEAD-L leaderboard (ranked on half the years, verified on
the other half) is the *current live config's immediate family*. Nothing structurally different
beats it. The only consistent improvement: **dropping the +40% take-profit entirely** (+1.30% →
+1.51% mean; the TP occasionally clips a monster). It's within the noise floor (SE ≈ ±0.5%), but
it's directionally free — the TP almost never binds, and when it does, it's wrong.

### F5. The numbers are honest about their own limits.
Split-half rank correlation across all 101k configs: **+0.65 (PEAD-L) / +0.62 (MOV-M)** — the
leaderboard *structure* is real, not noise-mining. But: MIDCAP is survivorship-biased (trust
direction, not magnitude — the 13x/110x portfolio numbers are shapes, not promises), 2025 was the
worst disco-proxy year in the sample, daily bars understate intraday whipsaw, and T+1 settlement
(cash account, GFV guard) will eat part of any velocity edge. Nothing here skips the paper gate.

---

## 2. The mean-vs-median dial (owner's tolerance choice)

All on the disco/movers cohort, all with stop12/sc8 protection. One row = one personality:

| Dial setting | Config | mean | median | win% | give-back | 6-slot port | Who picks this |
|---|---|---|---|---|---|---|---|
| **Max consistency** | TP8 | +0.71% | **+7.70%** | **60%** | **11%** | 5.3x | "I want most days green and almost no give-back" |
| **Quick-win (recommended)** | **TP10** | +0.87% | +9.70%¹ | 56% | 15% | 7.0x | The stated goals: quick wins, bank along the way |
| **Balanced compounder** | TP15 | **+1.03%** | −1.60% | 48% | 24% | **13.1x** | "Maximize account growth, accept red medians" |
| **Keep some upside** | 50%@10 trim + be10, no TP | +1.20% | +4.14% | 55% | 16% | 1.9x | "I can't stand fully exiting a runner" — costs slot velocity |
| **Max mean per trade** | let-run (pead style) | +1.03%² | −0.30% | 38% | 34% | 2.3x | Nobody, on this entry. This is what we're moving OFF |

¹ TP10's +9.7% median says the *majority* of movers touch +10% net within the hold — and let-run
gives it back (that 34% give-back rate is the owner's original complaint, measured).
² Identical mean to TP15 — on movers you give up *nothing* in expectancy by harvesting at +15%.

**The honest framing:** on disco there is no mean-vs-median trade-off to agonize over — TP15
matches let-run's mean exactly while compounding ~6x better at the account level. The real dial is
**median/win% (TP8–10) vs mean/compounding (TP15)**. TP10 vs TP15 is personality, not statistics.

On **pead** the trade-off is real and resolved the other way: harvesting costs ~0.5%/trade of true
edge. Don't harvest pead.

---

## 3. Recommendation — concrete config

**pead book (no behavior change, one optional tweak):**
```
STOP_LOSS_PCT=12  SOFT_CUT_PCT=8  TRAIL_BREAKEVEN_AT_PCT=12     # unchanged
TRAIL_STOP_PCT=15  TRAIL_ACTIVATE_PCT=20                        # unchanged
TAKE_PROFIT_PCT=40 -> consider OFF/raise to 60+                 # optional, +0.2%/trade suggestive
```

**disco book (the change — via the two-book v2.1 per-book overlay, behind `BOOKS_ENABLED=1`):**
```
DISCO_TAKE_PROFIT_PCT=10        # primary recommendation (the owner's stated goals)
DISCO_SCALE_OUT_TIERS=          # empty — scale-out is dominated under binding capital (F2)
# protection stays global: stop12 / softcut8 / be12 (F3)
# if total-return priority wins the day instead: DISCO_TAKE_PROFIT_PCT=15
```

**IMPLEMENTED 2026-06-10 (same session):** the per-book TP overlay is live in code —
`apply_decision.py` (paper, applies immediately), `live_execute.lot_take_profit_pct()` (gated
behind `DISCO_EXITS_LIVE=1`), `tick_context.py` (caps plumbing + book-aware fallback %-rule).
`.env` set: `DISCO_TAKE_PROFIT_PCT=10`, `DISCO_EXITS_LIVE=0` → **paper is validating the +10%
disco harvest as of now; live behavior unchanged.** Unit-tested
(`test_lot_take_profit_pct_per_book_overlay`, suite 150/150). DD prompts carry the two entry
heuristics (mega-cap-downtrend skip, gap-size ranking). Deferral telemetry already existed.

**Rollout (unchanged discipline):**
1. Paper first: ≥30 disco round-trips with the TP, judged via `pnl_report.py --by-book` against
   the let-run history. The backtest question is settled; the universe question only paper answers.
2. Log slot occupancy + GFV-guard deferrals (the velocity edge only exists if slots actually bind —
   confirm with live telemetry; if disco never fills its slots, the per-trade view favors TP10–12
   anyway, so the recommendation is robust either way).
3. Disarm rule stands: ≥30 round-trips with negative net expectancy, or the book tripwire twice →
   `BOOK_DISCO_ENABLED=0`.

**Entry-side bonus findings** (not exit, but the grid surfaced them):
- The pead edge concentrates in **gaps ≥10%** (+0.31%/trade at 7–10% gap vs +1.94% at 10–15%):
  when DD slots are scarce, rank pead candidates by gap size.
- **Mega-cap movers bought in a downtrend (SPY<50dMA) lose under every exit tested** — that's an
  entry mistake no exit fixes; worth a line in the DD prompt.
- Disco's harvest needs no regime switch (TP10 is the *best* risk-off policy on movers).

---

## 4. What was tested (for the record)

| Phase | Strategies | Evaluations |
|---|---|---|
| Hand-curated sweeps (§6a–6b) | 26 | ~100 |
| Backlog campaign, 12 axes (§6c–6d) | ~36 new | ~800 |
| Full factorial (§6e) | **101,520** | **~557,000** |

Axes covered: stop {8,10,12,16,off} × softcut {6,8,10,off} × breakeven {off,10,12,15} × TP
{6,8,10,12,15,20,25,30,40,off} × trail {off + 8 width@activation combos} × scale-out {off + 16
tier ladders}; entries PEAD (LARGE+MIDCAP) and movers (MIDCAP); plus hold {5,8,10,15,20}, gap×vol
entry grid, costs to 40bps/leg, whole-share lots at $310, SPY-regime splits, year-by-year
stability, and capital-constrained portfolio sims at 2/6 slots. Harness fixes that made the
numbers trustworthy: entry dedupe, paired bootstrap CIs, give-back peak fix, split-half validation.

---

## 5. Addendum — 2026-06-11 follow-up tests

### A1. Fine TP curve: 11–13 is a dead zone, not a compromise.
Ran the missing TP rungs (9, 11, 13, 14) through the same harness (stop12/sc8/be12, MOV-M).
The median cliff sits exactly at 10→11 (+9.70% → +3.45%): the typical mover tops out just above
+10%, so TP10 is the last rung the majority actually fills. TP11 is strictly worse than TP10
(mean +0.82% vs +0.87%, median collapsed, same port 7.0x); TP12's +0.04% mean edge is noise
(SE ±0.5%). The dial has two local optima — **TP10** (median/win%) and **TP14–15**
(mean/compounding, port 9.4–9.6x) — and nothing useful between them. Sharpe is flat (0.075–0.085)
across the whole family; the differentiators are shape metrics and slot velocity (4.9d → 7.0d).

### A2. trail12@12 is a near-miss salvage rung — include it on the TP15 dial only.
With be12 in place, a 12%-wide trail activated at +12 only exceeds breakeven once peak > 13.6%
(peak × 0.88 > entry), and TP15 fires at +15 — so its entire jurisdiction is trades peaking in
(13.6%, 15.0%) that then retrace. Measured: exactly 47/1840 trades (2.6%), hybrid better on 46/47
(mean on those −0.04% → +0.25%). Paired bootstrap on all trades straddles zero (+0.008%/trade,
CI [−0.011, +0.020]) — per-trade invisible — but the config is **weakly dominant** (floored by
be12, capped by TP15, can't lose in daily bars): it wins at every K 4–30 and in all 23
leave-one-year-out jackknives (+4.2–4.8% terminal at K=6). Caveats: ~2 binding trades/yr, and
intraday whipsaw (a trail 1.4pts under the TP) will eat part of it — live magnitude plausibly
half. **Under TP10 it binds on 0/1840 trades (TP fires before activation) — geometrically
irrelevant to the current paper config.** If the dial ever moves to DISCO_TAKE_PROFIT_PCT=15,
add DISCO_TRAIL_STOP_PCT=12 / DISCO_TRAIL_ACTIVATE_PCT=12 with it.

### A3. Gate-flip grandfathering: flipping DISCO_EXITS_LIVE=1 harvests instantly.
Any live disco lot already past +10% gets sold on the first tick after the flip — not gradually.
The flip is cheapest when few lots sit above target; check the blotter before arming. (Owner
decision 2026-06-11: existing live lots stay on let-run until the paper gate clears — live runs
ONE strategy at a time so the by-book comparison stays clean; no manual mid-flight harvests.)

### A4. Rollout step 2 is answered: capital BINDS live.
Engine log shows 243 entry-deferral events; recent ticks defer on "no settled cash (need $58–177 >
$4 left)". The slot-velocity premise of the TP recommendation is confirmed by live telemetry, not
just the sim. T+1 settlement (cash account) is the binding channel, as F5 predicted.

### A5. Washout-reversal entries (the UNFI shape) backtested — no validated edge; new H3 + label.
First-ever cohort for gap-DOWN + recover-to-top-of-range + heavy-volume entries (UNFI 6/9 was one:
gap −18.8%, range-pos 0.982, 9× vol). Strict shape (gap ≤ −10, rpos ≥ .8, vol ≥ 3x): LARGE mean
−1.7 to −2.1% under EVERY exit (n=23); MIDCAP median −3.4 to −6.7%, win 32–47% (n=38). Loose
(gap ≤ −7, rpos ≥ .7, vol ≥ 2x, n=105 MIDCAP): suggestively positive but let-run is the WORST
policy (+0.81%) and the TP family the best (TP15 +2.17%, TP10 +1.12% med +9.7%) — even this
shape's defenders shouldn't let it run. Conditional check on UNFI's actual spot (+12.8%, peak
+14.3%): movers that closed ≥ +12.8% mid-hold go on to mean +1.13% / median −0.40% / 31% full
give-back under let-run — hold-vs-sell is a per-trade wash; binding capital is what tips it.
Implemented (label-only, no behavior change): `dd_probe.washout_reversal` (gap ≤ −7 + rpos ≥ 0.7 +
vol ≥ 2x), plumbed through decide → lot/trade-log/catalyst-ledger like pead_qualified, and DD
prompt heuristic **H3** (not a validated entry; reject-or-downsize, never runner). Note: tagging
audit confirmed `route_book` works as designed — CBRL was the only pead_qualified=True verdict and
was correctly routed disco by the $30B floor; the pead book is empty for lack of qualifying
mega-cap gap-ups, not a routing bug.

### A6. FLIPPED LIVE — 2026-06-11, paper gate waived deliberately.
`DISCO_EXITS_LIVE=1` (owner decision, same day as A1–A5). Rationale for waiving the ≥30-paper-
round-trip gate: (a) paper can't model T+1 settlement, which is the velocity mechanism the TP
exists to exploit — the gate couldn't validate the thing it gated; (b) n=30 has a ±9pt SE on
win% — catastrophe detection only, and the disarm tripwire (BOOK_DISCO_ENABLED=0 on the
pre-committed criteria) already covers catastrophe; (c) paper exercises `apply_decision.py`, not
the gated `live_execute.lot_take_profit_pct()` path — unit tests cover that, paper doesn't;
(d) TP10 is a pure exit *tightening* — stops/softcut/be/sizing unchanged, so the worst case is
foregone upside, not new loss exposure. Known first act: UNFI (+12.8%, past the $49.34 TP)
harvests on the first market-hours tick — accepted as the policy acting, superseding the 6/10
"hold UNFI" call.
**Validation continues on live fills** via `scripts/exit_counterfactual.py` (NEW): let-run
replay (the pre-flip schedule, from .env) on the identical entries/fill prices from
data/trades.jsonl — a paired comparison with no venue/cadence/sizing confounds, strictly better
than the paper-vs-live read. Judge after ~30 live disco round-trips: if the mean delta
(actual − let-run) is negative net of the velocity benefit (deferral telemetry), revisit the
dial; tripwire criteria unchanged.
