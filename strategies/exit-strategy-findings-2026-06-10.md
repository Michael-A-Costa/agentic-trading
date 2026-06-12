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

### A7. Runner-regret follow-up (VELO 6/11): two untested exit families measured.
Trigger: VELO harvested at +15.1% (gap through the +10 TP) and kept running. Reality check first:
TP15 would have sold the *same tick* (fill 26.1254 ≥ 26.105); only trail/let-run schedules would
still hold it. Two families the 101k grid never covered, run through the same harness
(stop12/sc8/be12, MOV-M n=1840, split-half checked):

**(a) Tight trail at the TP level instead of a TP** ("ratchet the stop to just under price at
+10"): the grid's narrowest trail was 8% and was never activated at 10. Measured: tr2@10 mean
+0.84% (vs TP10 +0.87%), **port 8.70x vs 7.01x under the same whole-slot accounting** — an
accounting-robust improvement — but median collapses +9.70% → +2.22% (the trail arms end-of-day
in daily bars; overnight gap-downs eat the lock). Wider trails are strictly worse (tr8@10: 2.26x).
Live behavior would differ in both directions: 5-min re-arm + resting GTC stop ≈ intraday lock at
peak×0.98 (better than the sim), but overnight gaps remain (the "no downside" intuition is wrong
precisely there). Honest read: ties TP10 on mean, beats it on compounding, loses the median/
consistency profile that motivated TP10.

**(b) Moonshot-remnant ladder** (sell 75% at +10, remnant rides be12-floored): the tier grid
only ever tested 50% fractions. Measured: mean **+1.04%** (beats TP10 on BOTH split halves:
A +0.73 vs +0.60, B +1.41 vs +1.19), win 56%, give-back 16%, median +4.86% — TP10's consistency
with let-run's mean. Portfolio: 2.10x under whole-slot accounting BUT **12.58x under
partial-release accounting** (each sold fraction frees its capital on its fill date — the model
that matches live, which sizes from settled cash, not slots; verified: single-leg configs produce
identical numbers under both accountings). **This partially overturns F2**: "scale-outs are
dominated at the account level" was an artifact of whole-slot accounting; live frees 75% of the
cash at the tier fill. Adding the A2 salvage rung to the remnant (75%@10 + tr15@12): mean +0.95%,
port_pr 11.09x. Caveats: partial-release sim is new (one consistency check, no jackknife yet);
T+1 and intraday whipsaw unmodeled as ever (F5).

**Candidate dial if runner-regret is binding:** `DISCO_SCALE_OUT_TIERS=10:0.75` + remnant on
be12 floor (+ optional `DISCO_TRAIL_STOP_PCT=15 / DISCO_TRAIL_ACTIVATE_PCT=12`). Requires
plumbing the per-book overlay for tiers/trail (same pattern as `DISCO_TAKE_PROFIT_PCT`;
tick_context parses only the global `SCALE_OUT_TIERS` today). Not flipped — owner decision;
n=1 regret is not a tripwire, and the A6 counterfactual adjudication (30 round-trips) stands.
Repro: `python3 scripts/backtest_remnant.py` (tight-trail family: same harness, trail 2–8 @ act 10).

### A8. Intraday-only (flat by close) tested — it forfeits the edge. Overnight IS the edge.
Owner question 6/11: avoid overnight gap risk by going intraday-only. Measured on MOV-M (n=1840,
daily-bar decomposition of every held day into close→open and open→close legs):
- The 15-day drift splits **+1.99% overnight / +1.66% intraday** — cutting overnight exposure
  forfeits 55% of the gross edge up front.
- The intraday remainder is not harvestable: day-1 intraday-only (buy next open, TP10/stop8,
  flat at close) earns **+0.03% mean, median −0.22%, win 48%** — zero after costs (matches the
  2026-06 backtest_signal verdict: the entry is anti-predictive at the 1-day horizon). Tighter
  intraday brackets are worse (TP5/stop3: −0.33%). Re-buying daily to collect the +0.11%/day
  intraday legs costs 30bps/day in round-trip fees — deeply negative.
- Case in point: VELO entered 22.70 intraday 6/10, closed that day 22.68 (−0.1%). **The entire
  +15.1% realized came from the overnight gap into 6/11.** Flat-by-close scratches the trade.
Overnight gap risk is the *price* of the multi-day drift edge, not a removable defect; the
protections are sizing (conviction tiers), stop12, and the daily-loss breaker — not flattening.

### A9. FLIPPED LIVE — 2026-06-11: disco moonshot-remnant ladder replaces the TP10 full exit.
Owner decision (same day as A7/A8, prompted by the VELO regret): disco lots now harvest **75% at
+10** and let the **25% remnant ride a 3% trail from peak** (activated at +10), floored by be12,
capped by the global TP40. Per-trade the width barely matters — remnant trails 2–5% are a
statistical tie (mean +0.85–0.86%, identical median/win/give-back; port differences within noise)
— so the width is set from VELO's tape, on the CORRECT window (owner correction, same day: the
first calibration used the −3.9% open shakeout, which happens *below* +10 where the remnant trail
doesn't exist yet). Post-activation, VELO's pullbacks were ~2.5%: a 2% trail stops out on the
first wiggle (would have exited VELO at +19% — fine, but the moonshot ends), 3% rides it (locked
+17.8% at last check), 5% locks less (+15.4%). **Width = 3** ("just under current price" without
dying to hourly noise). Known bias: daily bars understate intraday whipsaw, so live 3% will exit
remnants somewhat earlier than the sim shows — acceptable, the remnant is profit-locking by design.
Looser (8%+) remains out: port_x collapses to 4.4x. Ladder economics vs TP10 unchanged: port ~9x
vs 7.0x, win 56%, give-back 15%.

Implementation (all suites green, 171 assertions / 41 tests):
- `tick_context.py`: `scale_out_tiers(env_key)` parameterized; `DISCO_SCALE_OUT_TIERS` ladder
  selected per-book in the exit screen (same live-gate as the TP overlay); `DISCO_TRAIL_*` in caps.
- `live_execute.py`: `trail_stop_price` is book-aware (disco rides the tight rung once
  `DISCO_EXITS_LIVE=1`); `execute_sell` partial-tier bookkeeping (marks `scaled`, remembers
  `init_qty`, ratchets to breakeven after the first trim, flags the remnant synthetic until
  reconcile re-arms the resting stop at the new qty) + a fix: `closing_order_id` is now only set
  on FULL closes, so a remnant's later stop-out books correctly as an external closure.
- Whole-share lots floor the trim (4sh → sell 3 keep 1); a 1-share lot degrades to a full TP at
  the tier. Paper has no trail mechanics, so the paper remnant rides be12-floor only — which is
  itself the best per-trade config (A7); live fidelity gap noted.
- `.env`: `DISCO_TAKE_PROFIT_PCT=0`, `DISCO_SCALE_OUT_TIERS=10:0.75`, `DISCO_TRAIL_STOP_PCT=3`,
  `DISCO_TRAIL_ACTIVATE_PCT=10`. Grandfathering checked at flip time: no live disco lot above
  +6.6%, so no instant trims and the TP retarget (+10 → +40 cap) lands before anyone reaches
  the tier. 1-share lots (SJM/ELF/BKH/NWE) will full-exit at +10 by design.

**Pre-committed restraint:** this supersedes the TP10 dial *once*; no further exit-dial changes
until ~30 live disco round-trips are scored by `exit_counterfactual.py` (now the paired judge of
ladder-vs-let-run on identical entries). Tripwire criteria unchanged (`BOOK_DISCO_ENABLED=0`).

### A10. Sentinel tier trims (1-min harvest latency) + a dead-sentinel bug found and fixed.
Owner-approved follow-up to the cadence question (the answer to which was: do NOT front-load
opening ticks — entries gain nothing (A8), downside is already broker-side, and faster ticks
would sample the trail's high-water tighter exactly when whipsaw peaks, shaking out the remnant
the ladder exists to keep). The one channel where speed purely helps is the tier trim — an
upside trigger only the engine can see — so `live_sentinel.py` now watches the scale-out tier on
ALL lots every minute (`_tier_breach`, same double-read confirm) and fires the trim through the
unit-tested `live_execute.execute_sell` partial path, with the lot's `scaled` marker as the
natural no-refire guard and `trade_log.record_fills` booking the row (a trim never makes the
position disappear, so reconcile's closed-external path can't book it).

**Bug found during the work: the live sentinel's breach detection had NEVER fired.** `_breach`
read `q.get("last")` but `dd_probe.cboe_quote` returns raw Cboe keys (`current_price`) — the
price was always None, so every synthetic-stop/TP breach was silently invisible since the
sentinel was built. No realized harm (all current lots are whole-share with resting broker stops;
the synthetic layer never had to catch anything), but a fractional lot would have been naked
between planner ticks. Fixed via `_quote_last()` (reads `current_price`, falls back to `last`),
regression-tested in the new `test_live_sentinel.py` (the quote-key regression, tier
fire/no-refire/gating, pead non-interference). Dry-run verified against live state.

**OCO question (owner, same day): can the tier rest broker-side next to the stop?** No —
Robinhood confirms it does not offer OCO/bracket orders (the MCP exposes only
market/limit/stop_market/stop_limit), and unlinked stacked sells conflict on shares
(`shares_available_for_sells` — the ALOY stranded-limit failure mode). Splitting (stop on the
remnant, limit on the harvest shares) leaves 75% of the position with no downside protection —
rejected per F3. The asymmetry stands: stop broker-side (gaps kill), harvest engine-side.
**Revisit trigger: if the Robinhood MCP ever adds OCO/bracket order types, move the tier to a
true resting limit.**

**Second sentinel data-quality fix (same session): detection now runs on REAL-TIME quotes.**
`dd_probe.cboe_quote` serves the ~15-min-DELAYED Cboe CDN feed unless the planner's quote cache
is <3 min old — so the sentinel's "1-min latency" was fictional on its old data path. It now
batch-fetches the broker's own real-time marks via `rh_direct.quotes` (~0.3s, $0, no LLM, one
call per pass) with per-symbol Cboe as fallback only. Verified live: 0.28s for the batch. Net
effect of A10: tier detection latency went from ~5 min on possibly-15-min-stale data to ~1 min
on real-time data — functionally equivalent to a resting limit at the tier, minus only the
sub-minute spike-and-retrace, while the full position keeps its resting stop.

**RH-everywhere audit (owner directive, same day): every PRICE a live decision reads is now
real-time RH.** Planner sweep (QUOTES_PREFER_RH=1, pre-existing), sentinel (this session), broker
truth (always), and now the DD probe: `dd_probe._rh_live_quote` overlays real-time
last/bid/ask/prev_close (via `mc.fetch_robinhood_direct`) on the delayed Cboe quote, so spread /
extension / "not yet extended" / intraday-% judgments see the live tape; sources telemetry gains
`rh_live`. Deliberately still keyless (no RH equivalent exists in the MCP): Cboe STRUCTURE fields
(volume, open/high/low, iv30 — confirmation signals where 15-min delay is tolerable), daily
HISTORY bars (no RH history endpoint; daily granularity), and the Nasdaq movers SCREENER
(no RH screener; it only nominates candidates — everything price-sensitive downstream re-checks
on RH). Yahoo/Stooq remain deep fallbacks only, never primary in live.

### A11. First live remnants (6/12) — the high-IV remnant got wicked out; vol-scaled-trail hypothesis.
The ladder's first three live harvests all fired at the 6/12 open (sentinel tier trim, +10 tier):
ALOY, CAVA, UEC. The remnants then diverged, and the split tracks entry IV almost perfectly:

| Sym | Entry | IV30 (buy thesis) | Remnant | Trail rungs | Outcome | Price now |
|-----|-------|-------------------|---------|-------------|---------|-----------|
| ALOY | 13.57 | **130%** | 2 sh | 13.57→14.65→14.96→15.17→**15.89** | **stopped out 09:50** (~3% off ~16.38 peak) | 16.33 |
| CAVA | 81.00 | (not flagged) | 1 sh | 81→87.33→87.77 | still riding | 90.93 |
| UEC  | 9.90  | (not flagged) | 3 sh | 9.90→10.57→10.68→10.81 | still riding | 11.22 |

ALOY — the only IV30=130% small-cap — spiked to ~16.38, gave back 3% to tag $15.89, and lost the
remnant **20 min after the trim, right before continuing to 16.33+**. CAVA/UEC ground higher
smoothly and their remnants are intact. This is the **first live confirmation of A9's pre-stated
bias** ("daily bars understate intraday whipsaw → live 3% exits remnants earlier than the sim")
— and it adds a conditioning variable A9 didn't have: on a 130%-IV name a 3% trail sits *inside*
the noise band, so the rung that's meant to ride the moonshot gets whipsawed out of it. Note the
direction: **tightening (the 2% we rejected on 6/12) would have wicked ALOY even sooner** — the
fix, if any, is a **vol-scaled / IV-conditioned trail** that *widens* the rung on extreme-IV
names, not a flat retune.

Caveats — do not act yet: n=1 wicked vs n=2 survived, all same-day, same open; the dollar miss on
ALOY's remnant is tiny (~$0.40/sh × 2 ≈ $0.80 vs current) and the lot still netted ~+$10.64
(~+11%) — the 75% harvest did its job. **This is a logged hypothesis, not a dial change.** Per
A9's pre-committed restraint, no exit-dial move until `exit_counterfactual.py` scores ~30 live
disco round-trips; this just flags IV30 as the variable to split that sample on when it's scored.

### A12. Remnant-trail instrumentation + PRE-REGISTERED variant grid (2026-06-12, no dial change).
The A11 validation plan as written could not have been executed: entry IV30 was never a
structured field in `trades.jsonl` (it only appeared incidentally in free-text DD reasons, and
post-hoc backfill reads post-crush IV — the wrong number), and `exit_counterfactual.py` replays
daily bars, which literally cannot see the ALOY wick (peak 16.38 → tag 15.89 → resume is
invisible at daily resolution). Three instrumentation pieces shipped — none touches a dial:

1. **Entry-time vol is now recorded.** `decide.py` carries the probe's `iv30` +
   `realized_vol_20d_annual_pct` (as `rvol20`) onto every commit action; `live_execute.py`
   stamps them on the lot and the fill result; `trade_log.py` persists them on every buy row.
   Pre-existing lots/rows stay vol-less (honest n/a in the replay).
2. **The 1-min sentinel now persists its quote pass** to `data/quotes-intraday.jsonl`
   (`{ts_utc, ts_et, quotes:{sym:last}}`, one row/min, all held lots, ~400 rows/session). It was
   already batch-fetching real-time RH marks every minute and discarding them; the tape is the
   only data that can replay remnant-trail variants without the daily-bar whipsaw bias (A9's
   known bias, A11's live confirmation). Recording verified live same-session.
3. **`exit_counterfactual.py --remnant`** replays every live disco scale-out harvest on the
   tape under a variant grid, with breakeven floor + TP40 cap on all variants, FIFO-pairing each
   harvest to ITS entry row for entry price + vol. Partial coverage is flagged (`TAPE GAP`), not
   silently scored.

**Pre-registered grid (frozen 2026-06-12, before any tape existed — do not extend after
peeking):** flat2 / flat3 (live) / flat5 / flat8; vol-scaled `w = clamp(k·IV30entry/√252, 3, 8)`
for k ∈ {1.0, 1.25, 1.5} (rvol20 fallback when IV missing); delay3@11ET (3% trail armed only
from 11:00 ET, breakeven floor before — targets the open-cluster whipsaw, all three first
harvests fired at the open). Rationale for vscale: in sigma units the flat 3% trail was ~0.4σ
on ALOY (IV 130 → daily σ ≈ 8.2%) vs ~1.3σ on CAVA/UEC — the width is denominated in the wrong
units, and the disco book *selects for* vol dispersion, so a single-tape (VELO) calibration
can't generalize. A9's "flat 8% collapses port_x" does not indict conditional widening: the
floor keeps normal-vol names at exactly today's 3%.

**Pre-registered decision rule (binding at the A9 checkpoint, ~30 scored live disco
round-trips):** adopt the variant that beats flat3 on mean remnant return over the tape-scored
sample; ties, insufficient tape coverage, or no winner → keep flat3. No interim dial moves;
tripwire (`BOOK_DISCO_ENABLED=0`) unchanged.

Addendum: the three pre-A12 entries are recoverable — `data/entry_vol_backfill.json` (sidecar;
the append-only ledger is never edited) carries ENTRY-TIME vol from probe caches written minutes
before each fill (CAVA 13:30 ET vs 13:46 entry; UEC 09:35 vs 09:37) and ALOY's 130% from the buy
thesis itself. The replay falls back to it, so the one wicked remnant — the motivating data
point — stays scorable on the vscale variants.

### A13. Breakeven-trigger + full trail×activate grid (2026-06-12, owner "+5% → breakeven" question, NO dial change).
Owner asked: once a lot is up +5–6%, lift the stop to breakeven / start trailing — "I don't want a
+6% win to drop to a −8% loss" (motivating live lot: HROW, disco, +6.4% @ 39.68 sitting on its
−12% hard stop, entry 37.30). Two sweeps run through `backtest_exit_policy.py` (curated be-rungs
kept in the file) and a new exhaustive grid, `backtest_trail_activate_grid.py`. Daily Cboe bars
(the §A12 caveat applies and is the crux below). All numbers net 15bps/leg, hold 15d.

**1. Breakeven-trigger sweep (be5/be6/be8/be12 on the live config) — a low breakeven is the WORST rung.**
The cost scales smoothly with how early it triggers; the earlier the trigger, the more winners it
amputates to scratches (win% collapse is the tell):

| rung | LARGE/pead mean·med·win | movers/disco mean·med·win |
|---|---|---|
| current (be12, **already live**) | +1.42% · +0.80% · 52% | +0.79% · −0.30% · 38% |
| be8  | +1.24% · +0.11% · 50% | +0.76% · −0.30% · 33% |
| be6  | +1.22% · −0.30% · 48% | +0.42% · −0.30% · 28% |
| **be5 (owner idea)** | **+0.96% · −0.30% · 46%** | **+0.32% · −0.30% · 25%** |

Verdict: be5 is dominated (costliest mean AND weakest protection — the wall sits at entry, ~5–6%
below current price, easily tagged). A breakeven ratchet must trigger HIGH (≥12); `be12` is ~free
on LARGE and is already live, applying to BOTH books (`live_execute.py:464`). So HROW is not naked
— its protection arms at +10% (disco ladder harvest) / +12% (be12), it just hadn't earned them at
+6.4%.

**2. Full trail×activate grid (288 combos: trail∈{3..20} × activate∈{5..20}, stop12/tp40/be12 fixed).**
Clean mean↔median diagonal on both books: **wide trail / late activation → max mean** (SE corner,
≈ today's let-run-ish behavior); **tight / early → max median & win%** (NW corner).
- **LARGE/pead — no free lunch.** trail3@act7 lifts median +0.80%→+2.13% but costs mean +1.42%→+0.82%
  (≈ −0.6%/trade). Monotonic, real, paid.
- **MIDCAP/disco — an APPARENT free lunch.** `trail3@act8` = mean **+0.78%** (≈ current +0.79%) with
  median **+3.42%** (vs current −0.30%). Same growth, give-back transformed. `trail4@act8` ≈ same.

**THE CATCH (why this is a hypothesis, not a recommendation):** the trail3 row is exactly where
daily bars lie most. A 3% trail only fires when a *daily low* prints 3% below the running peak —
daily bars are blind to the intraday wick that would trip it repeatedly live (the ALOY shape, A11).
So the disco "free lunch" is plausibly a daily-bar mirage, and it's the SAME 3%-width question §A12
is already collecting 1-min tape to answer — this session just adds the *activation* axis to it.

**3. Live counterfactual run (6/12) — 0 scored, as expected.** `exit_counterfactual.py --book disco`:
9 disco round-trips, 8 PARTIAL (opened <2d ago, replay still running), 0 scored. `--remnant`: 3
harvests (ALOY no tape; CAVA/UEC gappy) — all fired at the 9:30 ET open, BEFORE the A12 tape was
enabled (~10:20 ET, owner turned on sentinel market-tracking mid-session; commit `9bd2434` @ 10:26
ET). **Diagnosed: cold-start, NOT a bug** — sentinel tapes every 60s/all held lots/live-hours, so
coverage is automatic from the next open. The 6/12 harvests are PRE-TAPE → **excluded from the
§A12/§A9 30-round-trip count.** Faint hint only: flat2 already shows "too tight" (clipped UEC at
+11.22% where 3%+ rode to +13.34%); 3%+ indistinguishable on the short tape. Keep flat3.

**→ CHECK AT THE 30-ROUND-TRIP CHECKPOINT (§A9/§A12 binding decision):** when the tape has ~30
scored disco harvests, the §A12 remnant-width decision (flat3 vs variants) is the linchpin for BOTH
questions — if tight trails get whipsawed intraday, the disco trail3@act8 "free lunch" (§A13.2) dies
with flat3; if flat3 survives intraday, the whole-lot tight-early trail earns its own pre-registered
tape test. Do NOT adopt trail3@act8 (or any tight-early trail) off the daily grid. be5/low-breakeven
is settled (rejected, §A13.1) — no checkpoint needed. Repro: `backtest_trail_activate_grid.py`,
`backtest_exit_policy.py` (be-rungs + giveback rows).

### A14. OWNER OVERRIDE — be5 shipped LIVE despite A13.1 (2026-06-12, capital-preservation mandate).
Same day, owner directed: "if we're ever up 5% on a trade, raise the stop to breakeven — I just
don't want to lose money." This deliberately overrides A13.1's rejection (be5 costs ~0.5%/trade
mean): the account's standing objective is now capital preservation over expectancy, owner's call,
not to be relitigated (cf. §A6, the earlier deliberate gate-waiver). Shipped, NOT paper-staged
(owner accepts the EV cost; minutely sentinel + 4-min ticks make it cheap to revert):
- **`TRAIL_BREAKEVEN_AT_PCT=5`** (was 12), both books. Grandfathering checked at flip (§A3): 7 live
  lots had peak ≥+5%, all above entry → stops lifted to breakeven next tick, zero immediate sells.
- **Cooldown carve-out** (owner: "don't exclude stocks we broke even on if they turn back up"): a
  breakeven-or-better exit no longer starts the re-entry cooldown — only a realized LOSS does.
  `apply_decision.py` gates on `realized < 0`; `live_execute.py` reconcile gates on `stop_price <
  entry` (fill price unknown on that path). Tests added (suite 176/43).
- **Breakeven destination cushioned (owner, same day): `TRAIL_BREAKEVEN_OFFSET_PCT=1.0`** — the rung
  now lifts to `entry × 1.01`, not entry flat. "+1%, a good compromise": covers the ~0.3% round-trip
  cost (a stop-out is now TRULY no-loss, not a fee-sized loss) and locks ~+0.7% net. New live-only knob
  threaded through `tick_context._build_caps` → `live_execute.trail_stop_price`; backtest parity via
  `pol["be_off"]` in `backtest_exit_policy.simulate`. Must stay < `TRAIL_BREAKEVEN_AT_PCT`. Grandfather
  re-checked: all 7 peak-≥5% lots still above `entry×1.01` → no immediate sells. Tests: `test_breakeven_offset_lifts_above_entry` (suite 179/44).

**be5 and trail3@8 COMPOSE — they are not alternatives (the upgrade path).** The breakeven rung and the
trail rung stack (`live_execute.trail_stop_price` takes the highest engaged stop). So be5 (+offset) is the
trustworthy no-loss FLOOR shipped now; the §A13.2 disco `trail3@8` profit-lock layers ON TOP later, IF the
§A12 intraday tape confirms a tight trail survives at the 30-RT checkpoint. Head-to-head on daily bars
(stop12/tp40, 15bps): be5 = LARGE +0.96%/−0.30%/46%, disco +0.32%/−0.30%/25%; trail3@8 = LARGE
+0.80%/+1.98%/57%, disco +0.78%/+3.42%/57% — trail3@8 wins the BACKTEST (esp. disco) but is exactly the
daily-bar mirage (a 3% trail can't be seen at daily resolution), whereas be5's number is HONEST (a
breakeven stop sits far below price, cannot whipsaw on the upside). So: ship be5 blind (done), earn
trail3@8 at the tape checkpoint, then run BOTH (floor + profit-lock), not one-or-the-other.

### A15. OWNER SHIPPED trail3@8 LIVE — the profit-follow layer, with the be5+1% floor as the net (2026-06-12).
Same day, owner: "try out the trailing stop update as well, following the stock up as it increases."
So the §A13.2 trail3@8 that A14 said to "earn at the tape checkpoint" was instead shipped LIVE now —
the owner's reasoning being that the be5+1% floor caps the downside (worst case a whipsaw exits at
+1%, still green), minutely sentinel + 4-min ticks make it cheap to revert, and the upside (banking
runners) is wanted. Dials (all live `.env`, no code change — these are gitignored config):
- `TRAIL_STOP_PCT=3` (was 15), `TRAIL_ACTIVATE_PCT=8` (was 20) — base/pead trail.
- `DISCO_TRAIL_ACTIVATE_PCT=8` (was 10) — disco trail arms at +8 (width stays 3%; harvest tier stays
  `10:0.75`). So a disco lot now: +5% floor → +8% trail-follow (whole lot) → +10% harvest 75% →
  remnant rides the 3% trail.
Grandfather re-checked via `trail_stop_price` on fresh marks: 0 immediate sells (lots in +5–8% sit on
the floor; CAVA/UEC ≥+8% already trailed). **Two consequences to watch:**
1. **Daily-bar-optimistic** (the A12/A13 caveat is now live, not theoretical): a 3% trail will trip on
   intraday wicks and exit some runners early. The floor means those exits are still green. Watch
   whether realized give-back-protection beats the early-exit cost; widen (5–8%) if it over-trims.
2. **Partially preempts the §A12 remnant experiment.** A sharp +8→reverse now exits the whole lot on
   the trail BEFORE the +10 harvest fires → fewer remnants scored by `exit_counterfactual.py --remnant`.
   The pre-registered remnant-WIDTH test (flat3 vs variants) is unaffected for the harvests that DO
   fire (post-+10 remnant still trails 3%), but the sample will accrue slower. Not a contamination,
   a throughput cost — noted so the 30-RT checkpoint timing isn't mis-read as "stalled."
The §A13.1 verdict (be5 costs mean) and §A13.2 (trail3@8 is the daily-bar mirage) still stand as
research truth; A15 records that the owner chose to run them live anyway, floor-protected, ahead of
the tape — a deliberate, reversible experiment, not a refutation of the backtest.
