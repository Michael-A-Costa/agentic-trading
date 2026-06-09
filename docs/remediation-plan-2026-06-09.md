# Remediation Plan — 2026-06-09 audit

Source: full crosscheck of research/backtests (`research/signal-backtests.md`,
`strategies/catalyst-drift-v1-plan.md`, `scripts/backtest_exit_policy.py`, the landscape memo,
`data/research/best-traders-synthesis.md`) against the live config, logs, and broker state.

**Audit headline:** everything the backtests touched is well-built; the losses live in the paths
that bypassed them. Live is 2 days old, both days red (−$27.78 on 6/8, −$23.36 on 6/9), and nearly
all realized damage came through one unbacktested exit path. The fixes below restore the rule that
made the research good: **no mechanism trades real money until the harness has priced it.**

Priorities: P1/P2 are stop-the-bleeding (do before the next live session). P3/P4 make the live
experiment measurable. P5–P7 are hygiene. P8 is the standing decision rule.

## STATUS — implemented 2026-06-09 (same day)

| Item | Status |
|---|---|
| P1 | ✅ DONE — soft-cut layer modeled in `backtest_exit_policy.py` (Backtest 5 in `research/signal-backtests.md`). Verdict: softcut4 was destroying ~0.4%/trade (win 53→45 on LARGE); **softcut8 beats the plain config on mean AND sharpe in both universes** → re-enabled at `SOFT_CUT_PCT=8.0`; critical-band auto-sell fails the bar → `HOLD_RISK_CRIT_SELL=0` (band still drives re-DD cadence) |
| P2 | ✅ DONE — whole-share-or-skip already enforced in `size_entry` (verified + prompt hard rule added; no `MIN_POSITION_USD` floor per owner — the ≥1-share rule IS the dust guard). All 8 fractional dust lots exited 2026-06-09 ~14:40 ET via the engine path (`cleanup/fractional-dust`); book is now 100% whole-share with resting GTC stops |
| P3 | ✅ DONE — `pead_qualified` computed in `dd_probe.py` (gap ≥ `GAP_THRESHOLD_PCT`=5 AND rel_volume ≥ `VOL_MULT_MIN`=2; null fails closed), threaded through decide → actions → lots → `trades.jsonl` → `catalyst_events.jsonl`; labeling honesty rule in both DD prompts; `catalyst_filter_report.py` splits qualified-PEAD vs free-rein |
| P4 | ✅ DONE — `--mode` on `trade_ledger.py` + `pnl_report.py` (default `$TRADING_MODE`, else labeled MIXED); ledger dedupes order lifecycles; `pnl_report` open-positions block follows mode (live_state vs paper_state) |
| P5 | ✅ DONE — canary comment rewritten; dead `live_round_trip_done` stripped from state (no code read it); prompts templated `{MODE}` via `decide.prompt_text`; plan addendum added to `strategies/catalyst-drift-v1-plan.md`; memory updated |
| P6 | ✅ DONE — in-tick-confirmed buys log `status=filled` with real cost basis; reconcile books `filled`/`dead`/`closed_external` rows (engine-initiated exits marked via `closing_order_id`, no double-count); blotter flags `[placed — fill unconfirmed]` / `[NOT FILLED]`; readers dedupe by `order_id` |
| P7 | ✅ DONE — `test_reconcile_adoption_distinct_costs` pins per-lot stop/TP derivation (33/33 pass); F/SRAD was a real $15.17 coincidence, no bug |
| P9 (follow-up) | ✅ DONE — **regime gate split by evidence.** The old gate blanked ALL entries whenever SPY read risk_off, including the weeks-long downtrend override. Regime-split backtest (LARGE, n=56): downtrend-day PEAD entries kept full mean edge (+1.74%/trade vs +1.49% benign) but with lower win rate (48% vs 55%); acute-stress days looked toxic but n too small to trust. New behavior: **acute stress** (≤1 index green + VIX proxy +3%) still halts all entries; **confirmed downtrend** becomes PEAD-only mode — the screen admits only earnings-window candidates, `decide.py` deterministically suppresses any commit without `pead_qualified=True` (covers cached/flat-gap commits), and survivors size down ×0.6. Free-rein entries never trade into a confirmed downtrend (the trap the override was built for). |
| P8 | ✅ DONE — cumulative tripwire in `live_execute` (`LIVE_TRIPWIRE_BASELINE_USD=2064`, `LIVE_TRIPWIRE_PCT=10` → entries halt at equity ≤ ~$1,858, exits keep running, never resets overnight); June 26 evidence checkpoint recorded in the plan addendum |

---

## P1 — The soft-cut / critical risk-exit overrides the backtested exit policy

**Problem.** `hold_risk.py` sells at −4% ("soft-cut … & falling") and at risk ≥70 ("critical",
≈65% of the way to the stop ≈ −7.5%), regardless of conviction. It fired **14× on 6/9 alone**,
realizing −4% to −9% on day-0/1 of 15–21-day theses. `backtest_exit_policy.py` measured the exact
opposite: −12% stop is the robust optimum, tighter clips drift on noise, early exits collapse
Sharpe (tight trail → 0.05). Drawdown-then-drift is the normal shape of a PEAD trade (median gap
trade touches −8% and still drifts positive); the soft-cut treats it as failure. The discretionary/
risk-exit P&L bucket is −$15.79 at a 0% win rate; every other bucket nets positive.

**Fix (two stages):**
1. **Immediately:** set `HOLD_RISK_SELL=0` in `.env` (score-only mode — the risk score still
   drives the Tier-2 re-DD cadence, which is fine and cheap). Exits revert to what the backtest
   blessed: resting −12% stop + trail(15@20) + TP40 + `MAX_HOLD_DAYS` time-exit + manage-DD
   thesis exits.
2. **Then earn it back:** extend `backtest_exit_policy.py` with a soft-cut layer — daily-bar proxy:
   exit at the close of any day ≤ −SOFT_CUT% with a down close (and a separate "critical" variant
   at ~65%-to-stop). Sweep SOFT_CUT ∈ {4, 6, 8, none} × universes. Re-enable `HOLD_RISK_SELL=1`
   **only** for a cell that beats plain stop-12 on mean AND Sharpe. Document the verdict in
   `research/signal-backtests.md` either way.

**Files:** `.env` (one line now), `scripts/backtest_exit_policy.py`, then `scripts/hold_risk.py`
(retune `SOFT_CUT_PCT` / critical threshold to the winning cell, or leave disabled).

**Done when:** no live protective sells occur except broker stop / trail / TP / time-exit /
manage-DD, until a backtested cell justifies re-enabling; the backtest section is updated.

---

## P2 — Whole-share-or-skip + clean out the unprotected fractional dust

**Problem.** The ~$25 sizing floor forces fractional lots for any name >$25/share → fractional
can't carry a resting broker stop → synthetic tick-stop only → no overnight gap protection (the
plan says resting stops are *mandatory* overnight) → the soft-cut becomes the de facto risk
manager. Currently **8 fractional dust lots (~$160 total: OSCR, CRDO, GLXY, FUN, KLAC, ADEA,
AMAT, DUOL)** are held overnight with no broker stop. Each carries ~$0.30 expected edge per trade
while consuming manage-DD slots and re-DD spend.

**Fix:**
1. **Enforce whole-share-or-skip in code**, not just in the prompt: in the live entry path
   (`live_execute.py`), if `dollar_amount` buys <1 whole share at the live quote, either bump to
   1 share when that stays within `MAX_POSITION_USD`/headroom/settled-cash, or skip+log. Remove
   the fractional entry path from live (paper may keep it).
2. **Raise the floor:** set `MIN_POSITION_USD` so the minimum trade is meaningful (suggest ≥$75,
   owner's call) and let the "rounded down to nearest $25" rule in `dd_prompt.txt` follow it.
3. **One-time cleanup:** exit the 8 fractional dust lots at the next liquid session (marketable
   limits, log as `cleanup/fractional-dust` so they don't pollute the win-rate stats), freeing
   ~$160 settled by T+1.

**Files:** `scripts/live_execute.py` (sizing/entry), `.env` (`MIN_POSITION_USD`),
`scripts/dd_prompt.txt` + `dd_batch_prompt.txt` (state the whole-share rule as a hard rule),
one-off cleanup via the engine's exit path (not by hand, so it's logged).

**Done when:** every live overnight position has a confirmed resting GTC stop at the broker
(`get_equity_orders` trigger=stop count == open position count) and no live entry can create a
fractional lot.

---

## P3 — Stop PEAD label-stretching: gate the label on the measured signal

**Problem.** Free-rein entries are the owner's mandate, but the agent is borrowing the PEAD
label for non-signals: CCEP committed as "PEAD day-0" with a **−0.59% gap**; MASI with the
catalyst "unconfirmed". The measured edge requires **gap ≥5–7% on ≥2× volume** (t>2 only there;
it scales with gap size). A flat gap has no measured drift. 6/8's bounce/PT-raise entries are the
construction Backtest 1 proved anti-predictive — and they're exactly what got harvested at
−4..−9% on 6/9.

**Fix:**
1. In `decide.py`, compute `pead_qualified = (gap_pct >= GAP_THRESHOLD_PCT) and
   (rel_volume >= VOL_MULT_MIN)` from the DD packet (the knobs already exist in `.env`; un-deprecate
   them as *labeling* thresholds, not entry gates) and pass it into the prompt input.
2. In the prompts: `thesis_type:"earnings"/PEAD framing is only allowed when pead_qualified is
   true`; otherwise the agent must use its real label (momentum/reversal/news/…). Free-rein
   commits stay allowed — they just stop wearing the validated edge's badge.
3. Log `pead_qualified` per commit into `trades.jsonl` + `catalyst_events.jsonl` so the
   filter-lift report can split **qualified-PEAD vs free-rein** forward returns — that comparison
   is the cleanest test of whether discretion is adding or subtracting.
4. Optional, owner's call: a soft prior in the prompt — non-qualified commits size one tier lower.

**Files:** `scripts/decide.py`, `scripts/dd_prompt.txt`, `scripts/dd_batch_prompt.txt`,
`scripts/catalyst_log.py`, `scripts/catalyst_filter_report.py` (new bucket), `.env` comments.

**Done when:** every commit row carries `pead_qualified`, and `catalyst_filter_report.py` reports
the two cohorts separately.

---

## P4 — Split paper/live in the analytics (the win-rate question must be answerable)

**Problem.** `pnl_report.py` and `trade_ledger.py` blend paper and live rows: the ledger's
"still open" list doesn't match the broker, and the headline 41% win rate mixes the dead
pop-engine's paper fills with live PEAD trades. The whole live thesis is "does the agent's filter
lift win rate above ~45% unfiltered" — currently unanswerable from the tools.

**Fix:** add `--mode {paper,live,all}` (default **live** when `TRADING_MODE=live`) to both
scripts, filtering on the `mode` field already present in `trades.jsonl`; print the mode in the
header so a mixed report can't masquerade as live truth. Reconcile "still open" against
`live_state.json` lots when mode=live and flag mismatches.

**Files:** `scripts/pnl_report.py`, `scripts/trade_ledger.py`, note in `scripts/README.md`.

**Done when:** `python3 scripts/trade_ledger.py --mode live` open-lots list matches broker
positions, and live win-rate/profit-factor are reportable standalone.

---

## P5 — Stale flags, comments, and docs

All confirmed stale in the audit; each is a five-minute fix but together they erode trust in
the config as documentation:

| Item | Fix |
|---|---|
| `.env` `LIVE_ARMED` comment block references `LIVE_CANARY_USD` canary-capping — **no code references that variable** | Rewrite the comment to describe actual behavior; delete the canary claims (or reimplement the canary if wanted — decide, don't half-document) |
| `live_state.json` `live_round_trip_done` stuck `false` despite completed round-trips | Find the writer in `live_execute.py`; either fix the flip logic or delete the field if nothing reads it |
| `dd_prompt.txt` hardcodes "(PAPER mode)" while serving live decisions | Template the mode string from the engine (paper/live) so the agent knows real money is at stake |
| `strategies/catalyst-drift-v1-plan.md` gates say "pause live until paper-validated" while live is armed | Add a dated addendum recording the owner's decision to go live early + the P8 tripwires, so docs and reality agree |
| Memory `live-blocked-by-investor-profile` | ✅ already updated (RESOLVED 2026-06-08) |

---

## P6 — Journal honesty: placed ≠ filled

**Problem.** The 6/9 journal logs `BUY 4 CPB @ 22.06`, but the broker shows no CPB position and
no unsettled CPB sale — the order almost certainly never filled. `trade_log.py` records *placed*
live orders in the same voice as paper *fills*.

**Fix:** in the live path, log placement as `status: placed`, then on reconcile (the engine
already re-reads broker truth each tick) append a `status: filled @ avg_price` row (or
`status: dead` for cancelled/rejected/expired). Blotter renders unfilled orders distinctly
(e.g. `BUY 4 CPB — NOT FILLED`). The fill row, not the placement row, feeds P&L/win-rate stats
(coordinates with P4).

**Files:** `scripts/trade_log.py`, `scripts/live_execute.py` (reconcile hook),
`scripts/trade_ledger.py` / `pnl_report.py` (consume `status`).

**Done when:** re-running the 6/9 day shows CPB as not-filled, and ledger stats count only fills.

---

## P7 — Verify the adoption path (F/SRAD identical-state coincidence)

**Problem (low severity).** F and SRAD carry byte-identical entry/stop/TP in `live_state.json`
(both $15.17 / 13.3496 / 21.238). Broker confirms both avg costs at 15.17, so it's plausibly a
real coincidence — but both lots are `adopted: true`, and identical derived fields across two
adopted lots is exactly what a copy-bug would look like.

**Fix:** read the lot-adoption code in `live_execute.py`; add a unit test in
`test_live_execute.py` adopting two positions with different avg costs and asserting distinct
stop/TP. If a bug exists, recompute the affected lots' stops from broker avg cost and re-arm.

**Done when:** the test exists and passes; live stops re-verified against broker orders.

---

## P8 — Standing decision rule: the live experiment must have tripwires

**Problem.** Live went armed before the plan's own validation gate (filter-lift) produced a
single resolved event (144 logged, 0 resolved on 6/9; first cohort resolves ~**June 26**). Small
sizing makes this survivable, but an experiment without a pre-committed stop rule becomes a
slow bleed.

**Fix — adopt and write down (this section is that record, pending owner sign-off):**
1. **Budget tripwire:** if cumulative live realized+unrealized P&L hits **−10% of the 6/8
   starting equity (≈ −$206)** before the filter-lift report resolves, flip `LIVE_ARMED=0` and
   continue in dry-run + paper until the report justifies re-arming.
2. **Evidence gate (from the plan, now dated):** on ~**June 26** and weekly after, read
   `catalyst_filter_report.py`. Re-affirm live only if the REAL/qualified-PEAD cohort beats the
   gap-alone baseline; if after ≥30 resolved events the lift is absent, the book is a SPY-tracker
   — disarm and rethink (the plan's own words).
3. **Review cadence:** a weekly P&L + exit-type readout (now meaningful after P4/P6), checked
   against the backtest's expectations (+1.4–1.95%/trade, 42–51% win on qualified signals).
4. Whatever P1's backtest concludes about the soft-cut is adopted as-is — the harness, not the
   tick-by-tick discomfort of a red day, decides exit policy.

---

## Sequencing

1. **Today / before next session:** P1 step 1 (`HOLD_RISK_SELL=0`), P2 step 3 (dust cleanup
   queued), P5 `.env` comment fixes.
2. **This week:** P2 whole-share enforcement, P4 report split, P6 placed-vs-filled, P3 labeling.
3. **As research time allows:** P1 step 2 (soft-cut backtest), P7 adoption test.
4. **June 26:** first P8 evidence checkpoint.
