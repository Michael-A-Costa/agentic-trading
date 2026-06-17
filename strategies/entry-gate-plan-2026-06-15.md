# Entry-Gate Quality — Plan (2026-06-15)

**Status:** Phase 0 RUN — see **PHASE 0 RESULT** below. No dials changed. Companion to
`exit-strategy-findings-2026-06-10.md` §A19.

---

## PHASE 0 RESULT (2026-06-15) — hypothesis REFUTED, no entry gate warranted

Built + tested `entry_quality_report.py` (and `test_entry_quality_report.py`), joined 76/87 broker
round-trips to their buy rows, split realized P&L by every entry attribute. **The thesis that "the −$61
lives at the entry gate" is wrong.** There is no actionable, non-confounded entry attribute that owns the
loss at the pre-registered bar (n≥20, realized<0, PF<1.0, survives drop-top-2). Zero gate candidates.

What the data actually shows:
- **The hypothesised culprits are the GREEN buckets.** `conviction=low` +$29 (PF 1.94, win 51%),
  `book=disco` +$39.50 (PF 1.71), `thesis=news` +$53 (PF 3.04), `hold_intent=swing` +$42. The free-rein
  discretionary entries I expected to be the leak are the profit centre. BRUN is a tail case, not the mode.
- **The only negative slice is a CALENDAR ARTIFACT, not an attribute.** `untagged` / `unset`-metadata
  (n≈23–25, −$4 to −$7) is **entirely 6/08–6/09** — the first two live days, before the
  conviction/thesis/book tagging pipeline existed. Everything tagged (6/10→6/15) is net **+$39.50**. You
  cannot gate on "was an early trade"; the tool now labels these "uncategorized — not gate-able".
- **The −$61 was an exit-LEG accounting view, not −$61 of bad entries.** Grouped by exit type,
  discretionary legs sum to −$61 and rails to +$96 — but a single profitable disco name books BOTH (banks
  the scale/TP leg, gives a little back on the discretionary remnant leg). Grouped by the **entry**, the
  book is **+$35 and no entry class is a real loser**. The §A19 "real lever is the entry gate" line
  over-read the exit-leg figure; this Phase 0 corrects it.

**Only watch-items (thin, under the n bar — keep logging, DO NOT act):** `thesis=momentum` (n=7, −$5.65,
PF 0.07) and `thesis=sector` (n=3, −$7, PF 0.00). If either accrues to n≥20 and stays negative, *that* is
the future gate target (down-tier momentum-chase / sector entries). Re-run `entry_quality_report.py` at
the next checkpoint.

**Decision:** Phases 2–3 below are **NOT triggered** — the plan's own gate says stop. The measurement was
the deliverable, and it says the entry gate is not currently broken. Net engine edge is real (+$35
broker-truth, +$39.50 since tagging). The phased design below stands as the playbook for if/when a
watch-item matures.

---

**Original plan (pre-Phase-0) retained below for the methodology + the decision rules.**

**Status:** PLAN ONLY. No code/dials changed by this doc. Companion to
`exit-strategy-findings-2026-06-10.md` §A19 (which closed the exit thread by proving exits are
not where the money is lost).

## Thesis — the loss lives at the entry, not the exit

Broker truth (reconcile_ledger.py, n=87 round-trips): **+$34 net realized**, but exit-type split shows
discretionary exits at **−$61**. Two independent analyses say the discretionary *exit timing* is a
near-wash, not the cause:

- **Let-run counterfactual** (`exit_counterfactual.py`): mean delta **−0.31%/trade**, median +0.10% — the
  hand-cut roughly ties the mechanical rail. 10 of 23 cuts genuinely *saved* capital on falling names.
- **Held-to-now** (broker exit price vs current): same-day exits net **+$4 left on table** (mean −0.19%);
  the headline "+$25 left on table" is ~85% market beta from the 6/9→6/15 rally, not exit skill.

If the exit is a wash but the position still booked **−$61**, the loss was **already baked in at entry** —
the discretionary sell was just the hand that closed a position that was a loser from the buy. BRUN is the
proof: its own entry note listed every disqualifier (*"low conviction for iv152, parabolic flag, no fresh
catalyst driving today's 19%"*) and we took a full lot anyway. The mistake was the buy, not the sell.

The §A19 guard is a **+$3 insurance patch** (blocks 2/69 historical cuts). It does not touch the 67 trades
where the −$61 is generated. **This plan targets those 67 — the entry gate.**

## The entry gate (control points, by file)

1. **`tick_context.py`** — the deterministic screen/gate that nominates candidates from the movers screen.
   First filter: what even reaches DD.
2. **`decide.py` + `dd_worker.py`** — Stage-2 DD (LLM). Sets `decision` (COMMIT/PASS), `conviction`,
   `thesis_type`, and `dollar_amount`. Where conviction → size is decided.
3. **`live_execute.py` `pack_entries` / `size_entry`** — admits candidates at their conviction-tiered
   notional (1.0× / 0.6× / 0.35× of `MAX_POSITION_USD` per CLAUDE.md) within the exposure/settled-cash caps.

Every buy row already logs the attributes we need to grade entries:
`conviction`, `thesis_type`, `pead_qualified`, `washout_reversal`, `iv30`, `rvol20`, `hold_intent`, `book`.

## Phase 0 — MEASURE before any dial (the owner's standing rule)

Per [[exit-policy-tuning]] / [[data-as-feature-not-prose]]: no entry-gate dial moves until the data says
*which* entries bleed. The −$61 is an aggregate; it must be decomposed before we touch the gate.

**Deliverable: `entry_quality_report.py`** — join broker-truth round-trips (`ledger_truth.json`) back to
the `trades.jsonl` **buy** rows (by symbol + nearest entry time, same matching pattern as the §A19
null-price backfill) to attach each realized $ to its entry attributes. Then split realized P&L by:

- **conviction** (high / med / low) — *primary hypothesis: low-conviction is the −$61.*
- **thesis_type** and **pead_qualified** (signal-class: measured PEAD/gap-drift vs free-rein discretion).
- **book** (disco vs pead vs untagged).
- **entry context**: `iv30` bucket (is IV>~120% the BRUN-class tell?), `range_pos_52w` / `dist_52w_high_pct`
  (chasing extended/parabolic names), intraday % at entry (chasing a +19% move).

Output: realized $, win%, avg win/loss, profit factor **per bucket**, ranked by total $ drag. This tells us
exactly which entry slice to gate — or proves the thesis wrong (e.g. if low-conviction is actually green).

**Gate to Phase 2:** do not proceed until one or more buckets clearly own the loss with a defensible n.

## Phase 1 — pre-register the read (freeze before looking)

Before running Phase 0, commit the decision rule (mirrors the exit thread's pre-registration discipline):

> Adopt an entry-gate change for a bucket only if, over ≥20 closed round-trips in that bucket, it is
> realized-$ negative AND profit-factor < 1.0 AND the result is not an artifact of 1–2 outliers
> (drop-the-top-2 check). Ties or thin samples → no change, keep logging.

This prevents curve-fitting the gate to the rally-flattered sample we have now.

## Phase 2 — candidate gate rules (hypotheses, each a CODED rule not prose)

Only the buckets Phase 0 convicts get a rule. Candidates, in priority order:

1. **Make conviction bind on size (or entry-at-all).** If low-conviction owns the loss: low-conviction DD
   verdicts force the 0.35× tier *or* PASS outright (not a normal lot). Today conviction is logged but a
   "low conviction" COMMIT still gets sized like any other — BRUN got a full lot. Smallest, highest-leverage
   change.
2. **Parabolic / extended filter.** Backtestable rule in `tick_context.py`: reject (or down-tier) a
   candidate whose intraday move > X% or `dist_52w_high_pct` inside Y% (chasing the top). Wire to the
   existing range fields, per [[data-as-feature-not-prose]] — a coded gate, not a sentence in the DD prose.
3. **Extreme-IV gate.** If `iv30` > ~120% correlates with the drag (the BRUN/ALOY-class froth), down-tier or
   require a *fresh* catalyst to enter.
4. **Fresh-catalyst requirement.** Distinguish a catalyst already in the gap (stale, BRUN's MSA) from one
   driving *today* — only the latter justifies a disco entry. Hardest to encode; specify last.

Each rule ships **gated** (env flag, default OFF) exactly like §A19, with its own backtest over the joined
history before arming.

## Phase 3 — implement + validate

- Build the convicted rule behind a flag; unit-test the gate logic.
- **Replay over history** (the join from Phase 0): how many past entries would it have blocked/down-tiered,
  and what was their realized outcome? Report blocked-set vs passed-set $ — same format as the §A19 config
  backtest. Arm only if the replay shows a real, non-outlier improvement.
- After arming, the entry_quality_report keeps scoring new entries by bucket to confirm the gate holds.

## Out of scope / non-goals

- Not touching exits — that thread is closed (§A19). The guard stays armed as insurance.
- Not adding fundamentals/news prose to the DD commit layer (that layer loses, [[data-as-feature-not-prose]]).
  Every new input must wire to a backtestable rule.
- No dial moves in Phase 0/1 — measurement only.

## Open questions

- Does the join have enough closed round-trips per conviction bucket yet (n≥20), or do we need more live
  sessions first? Phase 0 will report the per-bucket n.
- Is the drag a *selection* problem (wrong names reach DD) or a *sizing* problem (right screen, too big on
  low-conviction)? The conviction × thesis_type split separates these and decides whether the fix lives in
  `tick_context.py` (screen) or `decide.py`/`pack_entries` (size).

---

## E1. Opening-window extension gate — built + tested, SHADOW (2026-06-17, NO dial change)

**Trigger.** JBL post-mortem: 1 share bought 9:36 ET at $421.85 (day-0 PEAD, +9% extended), faded to ~$395
= the whole 3085→3060 mark-down. Owner: "be more careful buying pops — check our methodology."

**Replay** (`scripts/entry_timing_replay.py`, 97 broker-truth round-trips joined to their screen candidate;
outcome = realized pnl_pct, exit timing is an established wash so this isolates ENTRY quality):

- **Test 1 — extension:** non-monotonic. Edge lives in the **+3–10%** band (PF 1.7–1.9). Both tails lose:
  barely-moving 0–3% (PF 0.4) and over-extended **10%+ (n=32, −$6.9, drop-2-best −$42)**.
- **Test 2 — anchor/chase:** NO POWER. The marketable-limit cap already keeps ~93% of fills within 0.5%
  of screen; only 2 trips chased >1.5%. The "fills below screen lose" result is momentum-direction, not
  chase cost. Not pursued.
- **Test 4 — open throttle:** first 60m = −$42 / 31 trips (PF≤0.66); 60m+ = **+$51 / 66 (PF 1.96)**. The
  entire book's profit is post-opening-hour entries.
- **Cross-tab (the real finding):** the loss is the *intersection* — extended **AND** opening-window:

  |            | ext <10% | ext ≥10% |
  |------------|----------|----------|
  | first 60m  | −$7 / 13 | **−$35 / 18** |
  | 60m+       | +$23 / 52 | +$28 / 14 |

  Extended-but-late is fine (+$28); opening-window-but-normal is mild (−$7). So the fix is a tight
  extension cap **scoped to the opening window**, not a blanket extension cap or a blanket open throttle.

**What shipped (SHADOW).** `tick_context.open_window_extension_block()` (pure, unit-tested:
`test_open_gate.py`, 12 checks) called in the candidate loop. Knobs `OPEN_GATE_MODE` (shadow|enforce|off),
`OPEN_GATE_WINDOW_MIN=60`, `OPEN_GATE_MAX_EXT_PCT=6`. Default **shadow** = records would-blocks into
`screen.open_gate.blocked` every tick, filters NOTHING (zero order-behaviour change). `enforce` drops the
candidate before it consumes a DD slot. Verified live: `open_gate` emits in `context_latest.json`.

**Caveats.** (1) The 97 closed trips EXCLUDE today's still-open opening-burst names (JBL et al.), so the
−$35 cell is likely understated. (2) Key cell n=18 — strong prior, not proof. (3) Cap=6% is the starting
hypothesis (catches JBL's screen-time +9.2%); the shadow log calibrates the exact bar.

**Arming gate (pre-registered, do NOT loosen).** Arm `OPEN_GATE_MODE=enforce` only when, over the shadow
window PLUS the matured replay (today's burst closed + re-run `entry_timing_replay.py`): the in-window
extended cell is still realized-$ negative with PF<1.0 AND survives dropping its 2 best trips, AND the
would-block set's forward outcome is worse than the passed set. Recheck ~2026-06-24.
