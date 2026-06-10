# Exit-Policy Backtest Playbook & Results Log

**Purpose:** a self-contained record of every exit-policy backtest run on 2026-06-10 — the
question, the toolchain, the *exact* methodology, the results, the verdicts, and the honest
limits — written so **another agent can replicate these and scale out a much larger sweep.**

If you are that agent: read §Toolchain → §Methodology → §How to extend, then work the
§Backlog. Append new results to §Results log with the command + date. Do **not** silently change
the entry definition or the cost model — those are held fixed so every policy is comparable.

---

## 1. The question

The account runs two virtual books (`strategies/two-book-v2-plan.md`):
- **`pead`** — the one validated edge (catalyst gap-drift, mega-cap, t≈3.1). Wants a **let-run** exit.
- **`disco`** — free-rein discretion (everything actually traded day-to-day). No validated multi-day
  edge.

The owner wants the `disco` book tuned for **(1) downside protection, (2) quick wins, (3) a little
money along the way** — a *different objective* from the drift edge. These backtests ask: **which
exit policy best serves that objective**, and what does it cost in mean return vs let-run?

Key subtlety the metrics must capture: the owner's complaint is "we were up, then gave it back." So
we measure **median, win%, left-tail, and give-back rate** — not just mean.

---

## 2. Toolchain (three scripts, all in `scripts/`)

| Script | Role | Key entry points |
|---|---|---|
| `backtest_gap_drift.py` | Establishes the ENTRY edge + owns all shared infra: data loaders, universes, stats. | `load_bars`, `clean_bars`, `LARGE` (60 mega-caps), `MIDCAP` (40), `stats`, `median` |
| `backtest_exit_policy.py` | Sweeps EXIT policies that mirror the LIVE `trail_stop_price` schedule (stop / breakeven / trail / TP / softcut / time-exit). No scale-out. | `find_entries`, `simulate`, `evalpol`, `POLICIES` list, CLI |
| `backtest_quickwin.py` | **NEW (2026-06-10).** Adds tiered SCALE-OUT exits + the owner-goal metrics (`p10`, `gaveback`). Reuses the other two's loaders/entries. | `simulate` (scale-out aware), `evalpol`, `POLICIES` list, CLI |
| `backtest_sweeps.py` | **NEW (2026-06-10 PM).** The §11 backlog campaign as one command: tier-gain / trim-fraction / ladder grids, hold/entry/cost sensitivity, whole-share lots, SPY-regime split, year-by-year, protection grid, entry-quality split, capital-constrained PORTFOLIO sim. Imports (never redefines) the other scripts' entries + `simulate`. | `MODES` dict, `--mode all`, `--slots`, `--overlap` |

The two `simulate()` functions are independent reimplementations of the same live schedule; keep them
in sync with `live_execute.trail_stop_price()` if the live rungs change.

**Harness upgrades (2026-06-10 PM, this session — re-verified rung-by-rung against
`live_execute.trail_stop_price`):**
- `--dedupe` (quickwin) / default-on in sweeps: drops entries overlapping a still-open same-symbol
  trade (live can't re-enter a held name — COOLDOWN + already-holding). LARGE 361→344 entries,
  MIDCAP 601→522 (13% of MIDCAP "trades" were overlaps clustered on repeat-gappers). Directions
  unchanged; magnitudes shift a little. Pass `--overlap` in sweeps to reproduce the §6a/6b tables.
- `--boot N`: **paired bootstrap** 5–95% CI on each policy's mean diff vs the reference row (same
  entries, per-trade diffs — strips the shared entry noise). `*` = CI excludes 0. This replaces the
  qualitative "noise floor" caveat with a number.
- `peak` now counts fills (a tier/TP fill proves the price traded there) — `gaveback` was
  undercounted for trades that tagged a tier and exited the same day.
- `p10` is now an interpolated quantile (was a crude index).

---

## 3. Data

- **Source:** Cboe delayed-quotes CDN, daily OHLCV JSON:
  `https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/{SYM}.json` (keyless — the owner
  won't use an API key; Yahoo/Stooq are dead/gated). Cached under `data/history/{SYM}.json`; pass
  `--refresh` to re-pull.
- **Cleaning (`clean_bars`):** drop sub-$2 bars, then start the series *after* the last >80% overnight
  jump — strips Cboe's pre-IPO/SPAC ghost bars that fabricate 100,000% returns.
- **Universes (hardcoded in `backtest_gap_drift.py`):**
  - `LARGE` = 60 S&P-100-class mega-caps (~≥$40B). **The trustworthy universe** — the gap-drift edge
    (t≈3.1) was validated here. Proxy for the `pead` book.
  - `MIDCAP` = 40 mid-caps. **Survivorship + recency BIASED** (current listings only; dead names
    absent). Closest available proxy for the `disco` small/mid tape, but its absolute numbers (esp.
    mean) are optimistic — trust *direction*, not magnitude.
- **There is NO data for the real `disco` universe** (discretionary small/mid names the LLM picks).
  This is the central limitation — see §7.

---

## 4. Methodology (held FIXED across all policies)

**Entry (the edge, not under test):** scan every trading day per symbol; an entry fires when
`open/prev_close − 1 ≥ GAP_THRESHOLD` (default 7%) **and** `volume ≥ VOL_MULT × 20-day avg vol`
(default 2×). **Enter at the gap-day close.** (`find_entries` in `backtest_exit_policy.py`.)

**Simulation (`simulate`):**
- Walk forward up to `--hold` trading days (default 15 ≈ 3 weeks).
- **Daily bars only.** The protective stop ratchets off the **prior** day's high-water mark — the
  daily-bar analog of the live 5-min cadence (no peeking at today's high to tighten today's stop).
- **Conservative intraday ordering** (assume the adverse print first each day): gap-at-open stop →
  intraday stop → scale-out tiers → final-lot TP → soft-cut at the close.
- **Cost:** `cost_bps` per leg (default 15 = 0.15%). A plain round-trip = 2 legs (entry + exit). A
  scale-out charges entry once + `cost_bps × fraction` per sell leg (≈ 2× total — fair vs non-scaled).
- Residual-glitch guard: drop any trade whose `|hold-day return| > 300%`.

**The exit schedule (the part UNDER test), and its `.env` mapping:**

| Rung | Backtest `pol` key | `.env` knob | Meaning |
|---|---|---|---|
| Catastrophe stop | `stop` | `STOP_LOSS_PCT` | static floor at `entry×(1−x%)` |
| Soft-cut | `softcut` | `SOFT_CUT_PCT` + `HOLD_RISK_SELL` | exit at the close of a down day this % underwater & still falling (`hold_risk.py`) |
| Breakeven rung | `be` | `TRAIL_BREAKEVEN_AT_PCT` | once PEAK ≥ x%, lift stop to entry (one-time) |
| Trailing rung | `trail` + `activate` | `TRAIL_STOP_PCT` / `TRAIL_ACTIVATE_PCT` | once PEAK ≥ activate%, ride `trail%` below high-water |
| Take-profit | `tp` | `TAKE_PROFIT_PCT` (`DISCO_TAKE_PROFIT_PCT` per-book) | full-lot exit at +x% |
| Scale-out | `tiers=[(gain%,frac), …]` | `DISCO_SCALE_OUT_TIERS` (`"5:0.5"`) | sell `frac` of the ORIGINAL lot at +gain%, ride the rest |
| Time-exit | `--hold` | `MAX_HOLD_DAYS` proxy | close the remainder on day `hold` |

**Live whole-share rule (NOT modeled in the backtest, which uses exact fractions):** a scale-out trim
in live = `round(frac × shares)` whole shares, **min 1, skip if it rounds to 0**. The remnant is
always whole, so it keeps its resting broker stop. The backtest's exact-fraction idealization is a
valid portfolio-level proxy; a "whole-share rounding at $X equity" sensitivity is a §Backlog item.

---

## 5. Metrics (and how each maps to the owner's goals)

`evalpol` returns, per policy over all entries:

| Metric | Definition | Owner goal it speaks to |
|---|---|---|
| `mean` | avg net per-trade return | total $ (let-run maximizes; quick-win trades this down) |
| `median` | middle trade | **#2/#3** — the *typical* outcome; quick-win lifts this |
| `win%` | fraction with net > 0 | **#2** quick wins (frequency of a green close) |
| `sharpe` | per-trade mean/sd (NOT annualized) | risk-adjusted return |
| `p10` | 10th-percentile return | **#1** downside (the left tail / bad-trade severity) |
| `gaveback` | % of trades whose PEAK ≥ +5% but CLOSE < +2% net | **#3** — *literally* "we were up then gave it back" |
| `days` | avg holding period | quick-win should be shorter |

---

## 6. Results log

### 6a. Exit-policy sweep — `backtest_exit_policy.py` (LARGE, gap7/vol2, hold15, 361 entries, 15bps)

Trail-WIDTH curve (tightening the continuous trail) + the protection rungs. Mean per ~15d trade:

```
TIME-ONLY (no stop/TP)        +1.50%   (raw drift ceiling)
LIVE + softcut8               +1.48%   sharpe 0.141   <- best risk-adj
LIVE cfg (stop12/tp40/tr15@20)+1.43%
trail20 act0 (loose)          +1.00%
BASELINE (stop8/tp25)         +0.98%
trail10 act0                  +0.78%
trail8  act0 (tight)          +0.56%   <- tightest trail = worst
```
Fine grid on the breakeven trigger (on the live stop12/tp40/tr15@20 base):
```
LIVE                +1.43%  median +0.98%  sharpe 0.134
LIVE + be10         +1.36%  median +0.16%  sharpe 0.130   <- be10 BACKFIRES (median collapse)
LIVE + be12 +sc8    +1.47%  median +0.80%  sharpe 0.141
LIVE + be15 +sc8    +1.50%  median +0.94%  sharpe 0.144   <- optimum
```

### 6b. Quick-win / scale-out sweep — `backtest_quickwin.py` (gap7/vol2, hold15, 15bps/leg)

Shared protection on ALL rows: `stop12 / softcut8 / be12`.

**LARGE (mega-cap, trustworthy — proxy for `pead`):** 361 entries
```
policy                         mean   median  win%  sharpe   p10    gaveback  days
LET-RUN (tp40 tr15@20)        +1.53%  +0.80%   52%  0.143  -10.3%    12%      12.5
tight TP8                     +0.78%  +2.02%   57%  0.099  -10.2%     6%      10.1
tight TP10                    +0.83%  +1.30%   55%  0.099  -10.3%     8%      10.8
tight TP15                    +1.02%  +0.94%   53%  0.114  -10.3%    11%      11.8
scale 50%@5 + run             +0.98%  +1.88%   58%  0.118   -9.6%     7%      12.5
scale 33%@5,33%@8 + run       +0.92%  +1.79%   58%  0.114   -9.6%     7%      12.5
scale 33%@5,33%@10 + trail    +0.93%  +1.47%   57%  0.114   -9.6%     7%      12.5
scale 50%@8 + trail rest      +1.04%  +1.34%   55%  0.120  -10.2%     8%      12.4
```
**MIDCAP (BIASED — proxy for `disco`):** 601 entries
```
policy                         mean   median  win%  sharpe   p10    gaveback  days
LET-RUN (tp40 tr15@20)        +1.56%  -0.30%   41%  0.103  -12.3%    29%       8.3
tight TP8                     +0.71%  +7.70%   60%  0.076  -12.3%     9%       4.8
tight TP10                    +0.68%  +4.51%   55%  0.066  -12.3%    14%       5.6
tight TP15                    +1.23%  -0.30%   48%  0.103  -12.3%    22%       6.7
scale 50%@5 + run             +0.91%  +2.09%   55%  0.088  -12.3%    16%       8.3
scale 33%@5,33%@8 + run       +0.85%  +3.42%   59%  0.088  -12.3%    14%       8.3
scale 33%@5,33%@10 + trail    +0.84%  +3.21%   56%  0.084  -12.3%    14%       8.3
scale 50%@8 + trail rest      +1.07%  +2.87%   54%  0.098  -12.3%    16%       8.0
```

### 6c. Backlog campaign — `backtest_sweeps.py` (2026-06-10 PM; entries DEDUPED, 15bps/leg, hold15, gap7/vol2 unless stated)

All rows share stop12/softcut8/be12. LARGE = 344 entries, MIDCAP = 522 after dedupe.
Headline policies: **LR** = let-run (tp40 tr15@20), **TP10** = tight tp10, **SC5** = 50%@5 + run,
**SC8** = 50%@8 + tr12@12.

**Deduped headline + paired-bootstrap CIs** (`backtest_quickwin.py --dedupe --boot 2000`):
```
LARGE : LR +1.30% (ref) | TP10 +0.70 [-1.09,-0.17]* | SC5 +0.84 [-0.80,-0.17]* | SC8 +0.88 [-0.75,-0.12]*
MIDCAP: LR +1.91% (ref) | TP10 +1.10 [-1.52,-0.14]* | SC5 +1.26 [-1.08,-0.24]* | SC8 +1.48 [-0.93,+0.01]
        TP15 +1.68 [-0.82,+0.36]  (mean-indistinguishable from LR; median -0.23 though)
```
The mean cost of quick-win is REAL (CIs exclude 0) everywhere except **SC8 on MIDCAP** (borderline)
and TP15 on MIDCAP. SC8's medians: LARGE +1.24%, MIDCAP +3.54%; gaveback 29%→16%.

**Tier-gain grid** (`--mode tiergain --boot 2000`): later trims cost less mean and (on MIDCAP) raise
the median — `50%@8` dominates `50%@5` on BOTH books (MIDCAP median +3.34 vs +2.20, mean +1.51 vs
+1.26, gaveback 16% both). The earlier the trim, the more mean it burns (@3 worst).

**Trim-fraction grid** (`--mode frac`): clean monotone trade-off — more banked = higher median/win%,
lower mean. 50% is the knee on both universes; 75%@5 pushes MIDCAP win% to 69% at mean +0.93.

**Ladder grid** (`--mode ladder --boot 2000`): best ladder = `33%@6,33%@10` (MIDCAP median +4.23%,
win 58–63%, gaveback 14%) but its mean cost vs LR is significant [-1.14,-0.16]. Single-trim SC8 keeps
more mean; ladders buy a prettier median. All ladders ≈ equal on LARGE (~+0.8%).

**Hold sensitivity** (`--mode hold`, entry set re-found per hold): drift accrues with hold on BOTH
universes — LR mean rises monotonically 5d→20d (LARGE +0.40→+2.01, MIDCAP +0.43→+2.23). Quick-win
medians stay positive at every hold on MIDCAP. Nothing argues for shortening MAX_HOLD_DAYS; hold20
slightly beats hold15 for LR.

**Entry sensitivity** (`--mode entry`): on LARGE the edge scales hard with gap size — LR mean
+1.15% (gap5) → +1.30% (gap7) → **+2.53% (gap10)**, n=158. Vol-mult matters less (1.5→3 mild lift).
On MIDCAP entry definition barely moves anything (mean +1.4–2.0 everywhere, medians always ≈ -0.30
for LR / +2.2 for SC5). The best-exit RANKING never flips across the entry grid — exit conclusions
are robust to the entry definition.

**Cost sensitivity** (`--mode cost`, 10/15/25/40bps): rankings unchanged through 40bps/leg; every
headline policy stays mean-positive on both universes. Scale-out does NOT die at realistic costs
(it pays the same ~2 legs total as a plain round-trip).

**Whole-share lots** (`--mode lot`, $310 and $1000/position): quantizing trims to whole shares at
$310 does NOT erode scale-out — numbers match or slightly beat exact fractions (the 27 LARGE /
15 MIDCAP names too expensive for 1 share are skipped, a small selection effect). The §4 fractional
idealization is validated at live size.

**Regime split** (`--mode regime`, SPY>50dMA on entry date, keyless Cboe SPY):
```
LARGE  risk-on  (238): LR +1.38  TP10 +1.04  SC5 +0.91  SC8 +1.03
LARGE  risk-off (105): LR +1.16  TP10 -0.03  SC5 +0.72  SC8 +0.58   <- TP10 dies risk-off
MIDCAP risk-on  (408): LR +1.54  TP10 +0.87  SC5 +1.03  SC8 +1.33
MIDCAP risk-off (114): LR +3.25  TP10 +1.90  SC5 +2.06  SC8 +1.99   <- edge BIGGER risk-off
```
The drift edge survives risk-off (supports `REGIME_ENTRY_GATE=0`); a tight full-exit TP is the only
family that breaks (clips the vol-expansion winners that pay for risk-off losers).

**Year-by-year** (`--mode year`): SC5's median is positive in 12/16 LARGE years (LR: 8/16) and
10/12 MIDCAP years (LR: 3/12). 2022 bear: SC loses less than LR on LARGE (-1.75 vs -3.48 mean).
**Warning flag: MIDCAP 2025 is the worst year in the sample (LR mean -1.87%, median -8.77%; SC
-1.13%/-2.26%) and 2026 YTD is flat-to-negative** — the disco-proxy tape has been hostile for ~18
months. The backtest's all-history means are NOT a promise about the current tape.

**Protection grid** (`--mode protect`, re-tuned under both exit families): stop12 confirmed (8 too
tight, 16/OFF only "win" on MIDCAP mean via the survivorship trap — keep the insurance); softcut8
confirmed (sc6 worse, scOFF gives up the LARGE p10 protection -9.7→-12.3); breakeven rung ≈ FREE on
every config (be10/12/15/20/OFF within noise of each other after dedupe — keep be12, it costs
nothing and caps give-back psychology). The protection layer is NOT interaction-sensitive: the same
stop12/sc8/be12 is right under let-run and under scale-out.

**Entry-quality split** (`--mode quality`): gap-size buckets on LARGE — 7-10% gaps are nearly
edgeless (LR +0.31%); 10-15% is the sweet spot (LR +1.94%, median +2.10%, win 58%); 15%+ has the
fattest mean (+4.00%) on a lumpy median. **Most of the LARGE drift edge lives in gaps ≥10%.**
Strong-vs-weak gap-day close: weak closes drift MORE on LARGE (+1.59 vs +0.99) but LESS on MIDCAP
(+1.17 vs +2.50) — inconsistent, NOT actionable as a filter.

**Portfolio sim — capital-constrained compounding** (`--mode portfolio`, equity/K per entry, slot
freed on exit; single path, no pairing — read big gaps only):
```
            6 slots (capital rarely binds)        2 slots (capital BINDS)
LARGE : LR 1.67x  TP10 1.34x  SC5 1.36x  SC8 1.38x | LR 2.41x  TP10 2.10x  SC5 1.69x  SC8 1.71x
MIDCAP: LR 3.55x  TP10 2.33x  SC5 2.28x  SC8 2.67x | LR 2.88x  TP10 3.95x  SC5 2.61x  SC8 3.56x
                                                     ^ ranking FLIPS: TP10 takes 363 trades vs 296,
                                                       compounds best AND lowest maxDD on MIDCAP
```
At 6 slots the gap7/vol2 signal stream is sparse enough that ~96% of signals fit — per-trade mean
rules and LR wins. When capital binds (2 slots), **capital velocity beats per-trade mean**: on
MIDCAP, TP10 recycles into 23% more trades and compounds 3.95x vs LR's 2.88x. Live disco sees ~25
candidates/day — far denser than this backtest's stream — so live capital binds much harder than
the 6-slot sim. This is the strongest argument that the disco book should harvest, not let-run.

### 6d. DISCO-ENTRY test — `--mode disco` / `--entry movers` (2026-06-10 PM; the risky-mode cohort)

Everything above uses the PEAD gap+vol entry. The disco book doesn't enter like that — it buys the
day's TOP MOVERS from discovery (close-to-close gainers, NO gap or rel-volume gate, including pure
intraday runners). `find_mover_entries` simulates that screen: enter at the close of any day with
c/c gain ≥7%, deduped. This is the closest daily-bar analog of the live discovery feed.

**Per-trade, hold15, stop12/sc8/be12** (`--mode disco --boot 2000`):
```
                          LET-RUN              tight TP10           scale 50%@8+tr12@12
LARGE  movers (1101): +0.09% med -0.30 win 47 | -0.08% med +0.80 win 52 | -0.07% med +0.77 win 52
MIDCAP movers (1840): +1.03% med -0.30 win 38 | +0.87% med +9.70 win 56 | +0.81% med +2.89 win 54
MIDCAP gapless(1161): +1.33% med -0.53 win 38 | +0.91% med +9.70 win 55 | +0.94% med +2.78 win 53
```
- **The movers entry is far weaker than the PEAD entry** (LARGE +1.30% → +0.09%; MIDCAP +1.91% →
  +1.03%). The gap+volume qualification carries real signal; "it went up a lot today" alone is
  nearly edgeless on mega-caps.
- **On this cohort, let-run's mean advantage DISAPPEARS** — most quick-win mean-diff CIs straddle 0
  (e.g. TP10 on MIDCAP movers [-0.55,+0.23]). The fat-winner tail that pays for let-run on PEAD
  entries isn't reliably there on movers. Meanwhile win% 38→54-56, median flips hard positive,
  give-back 34%→15-19%.
- LET-RUN's give-back on MIDCAP movers is **34%** — worse than on PEAD entries (29%). The owner's
  complaint is maximal exactly where the account actually trades.

**Portfolio sim, movers stream, 6 slots** (`--mode portfolio --entry movers` — capital BINDS here:
LR takes 1192/1840 signals; this is the live-realistic configuration):
```
LARGE : LR 0.97x (maxDD 54%) | TP10 1.29x (34%) | SC5 0.90x | SC8 0.88x
MIDCAP: LR 2.29x (maxDD 72%) | TP10 7.01x (62%) | SC5 2.31x (54%) | SC8 2.83x (62%)
```
**On the disco-realistic stream, tight TP10 dominates let-run at the account level** (3x the
terminal equity on MIDCAP, lower maxDD, +300 trades taken; on LARGE movers it's the only policy
that compounds at all). Treat the 7x as survivorship-inflated magnitude, but the mechanism is
structural: when capital binds, recycling beats per-trade mean, and movers' median trade touches
+10% within the hold while its mean is mediocre — exactly the shape a tight TP harvests.

Full TP-family curve on the same stream (added 2026-06-10 PM while charting,
`scripts/plot_exit_backtests.py`): TP8 5.31x, TP10 7.01x, **TP15 9.36x**, let-run 2.29x, scale-outs
2.2–2.8x. The whole tight-TP family beats every scale-out at the account level; TP10-vs-TP15
ordering is single-path noise — read it as "the harvest sweet spot is a full exit somewhere in the
+10–15% band", not as TP15 > TP10.

**Regime on movers** (`--mode regime --entry movers`): the PEAD-cohort "TP10 dies risk-off" result
REVERSES here — TP10 is the *best* policy risk-off on MIDCAP movers (+0.55% vs LR +0.13%, win 54%
vs 34%). And LARGE movers risk-off is negative under EVERY exit (mean -1.5 to -1.8%) — buying
mega-cap movers in a downtrend just loses; no exit fixes a bad entry.

**Cash-account caveat:** TP10's velocity (5.4d avg hold) means more T+1 unsettled-cash churn — the
`CASH_SETTLEMENT_GUARD` will defer some re-entries, eating part of the recycling advantage. The
paper book doesn't model settlement; the live advantage is smaller than the sim's.

### 6e. FULL FACTORIAL megasweep — `backtest_megasweep.py` (2026-06-10 PM; 101,520 configs)

Every stop×softcut×be×tp×trail×tiers combination (axes in the script header), each on PEAD-L /
PEAD-M / MOV-M with **entry-year-parity split-half validation** and a 6-slot portfolio sim on
MOV-M. ~557k evaluations, 296s on 8 cores. Full results: `data/backtest/megasweep_results.json`.

- **Split-half rank correlation of config means: +0.65 (PEAD-L), +0.62 (MOV-M)** — the grid's
  structure replicates across disjoint year halves; the leaderboard families are real.
- **PEAD-L: the current live config's family IS the global optimum.** Top of both leaderboards =
  stop12/sc8/be10-12, trail15, **no TP** (+1.51% vs +1.30% with tp40 — the TP occasionally clips a
  monster; within noise but directionally free). Nothing structurally different beats it in 101k.
- **MOV-M raw "winners" are all no-stop configs (mean ~2x higher) — the survivorship trap,
  quantified.** Matched-config insurance premium of stop12/sc8: tp12 harvest +1.88%→+0.91%;
  50%@10 trim +2.52%→+1.49%. Keep paying it (delisted left tail is invisible to this data).
  stop8 is strictly worse than stop12 on every cohort.
- **Protected (stop12/sc8) MOV-M leaderboards:** per-trade median/win% peak = scale 50%@8-10 or
  TP8-10 (median +3.7-9.7%, win 55-60%, gb 11-16%); mean peak = TP15-20 (+1.03-1.26%, = let-run's
  mean); **6-slot portfolio peak = plain TP15 (13.1x) then TP20 (11.9x); every scale-out lands at
  only 1.7-2.7x** — the remnant occupies the slot for the full hold, so trims pay let-run's slot
  cost for half the position. Under binding capital, full-exit TPs dominate scale-outs.
- Protected TP-family curve on MOV-M (per-trade → portfolio): tp8 +0.71%/med+7.7/gb11% → 5.3x;
  tp10 +0.87%/med+9.7/gb15% → 7.0x; tp12 +0.91%/med+1.5 → 7.0x; tp15 +1.03%/med−1.6 → 13.1x;
  tp20 +1.26%/med−4.0 → 11.9x. The median cliff between tp10 and tp12 = the typical mover tops
  out around +10-11%.

### 6f. Slot-count sensitivity — `--mode slots` (2026-06-10 PM; owner challenge: "6 slots is low, we run 20-30")

Portfolio sim re-run at K ∈ {4,6,10,15,20,30} slots × 10 policies × 3 movers streams (LARGE,
MIDCAP, COMBINED = both merged, 2941 signals — densest, closest to "disco trades anything").
Cell = terminal equity; %u = signals taken. COMBINED stream:
```
                 K=4        K=6        K=10       K=15       K=20       K=30
LET-RUN        0.72x 43%   1.33x 56%  1.98x 73%  1.84x 85%  1.75x 92%  1.48x 96%
tight TP10    14.96x 55%   9.96x 69%  4.50x 84%  2.52x 92%  1.88x 95%  1.46x 97%
tight TP15    38.23x 49%  16.23x 63%  7.73x 80%  3.70x 90%  2.41x 94%  1.73x 97%
scale 50%@8    1.60x 44%   1.41x 57%  2.27x 73%  1.75x 86%  1.53x 92%  1.35x 97%
```
- **The TP-over-let-run ranking holds at EVERY slot count, 4 through 30** — the margin narrows as
  utilization approaches 100% (K=30: TP15 1.73x vs LR 1.48x) but never flips, because even
  unconstrained, TP15 earns the same per-trade mean in 6.7d that let-run takes 8.9d to earn.
- Scale-outs/ladders never beat the TP family at any K.
- **How K maps to live:** the sim's K = how many position-sized chunks capital divides into. Live
  caps positions at 15% of equity with a 95% exposure ceiling → ~6 full-size chunks; conviction
  tiers (0.6×/0.35×) stretch that to an effective K ≈ 10-15 even when 20-30 names are held
  (`MAX_OPEN_POSITIONS=30` is a name-count ceiling, not a sizing divisor). In the effective-K
  10-15 band on the combined stream: TP15 3.7-7.7x vs let-run 1.8-2.0x.
- LARGE-cap movers stream stays ≈1x for everything at K≥15 — that entry is edgeless; no exit or
  slot count fixes it (consistent with §6d).

**Synthesis + final recommendation:** `strategies/exit-strategy-findings-2026-06-10.md` (the
writeup: five findings, the mean-vs-median dial table, concrete `DISCO_TAKE_PROFIT_PCT=10` config,
rollout gates). §6f's slot sweep is folded into its F2.

---

## 7. Findings / verdicts (as of 2026-06-10)

1. **Keep `pead` on let-run.** On LARGE, let-run has the best mean (+1.53%) and sharpe (0.143). Every
   quick-win variant trades mean down. The validated edge wants patience.
2. **Tightening the winner-trail monotonically bleeds the edge** (+1.00% loose → +0.56% tight). A
   "chase the stop up tightly" scheme is the worst exit family. Do not propose it for `pead`.
3. **The breakeven rung is trigger-sensitive:** `be10` backfires (median +0.98%→+0.16%); damage clears
   at `be12` (+1.47%), optimum ~`be15`. Live set to **`TRAIL_BREAKEVEN_AT_PCT=12`** (2026-06-10) —
   catches a +14% peak like UNFI while staying near-optimal.
4. **On the disco-like tape (MIDCAP), let-run has a NEGATIVE median (−0.30%) and 29% give-back** — the
   data reproduces the owner's exact complaint. A quick-win exit flips the median strongly positive and
   roughly **halves give-back** (29%→14–16%), raising win% to 55–60%, at the cost of ~⅓–½ of the mean.
5. **Protection (p10) comes from the stop/softcut, not scale-out.** On MIDCAP, p10 is pinned at the
   −12% stop regardless of profit-taking. Scale-out buys goals #2/#3 (quick wins, less give-back), not
   deeper tail protection — which is already handled by the GLOBAL stop12/softcut8. Clean decomposition:
   **protection global, harvest per-book.**
6. **Current `disco` recommendation:** ~~`DISCO_SCALE_OUT_TIERS=5:0.5`~~ **SUPERSEDED by §7b.3
   (2026-06-10 PM): the trim belongs at +8%, not +5%.**

### 7b. Updated verdicts — backlog campaign (2026-06-10 PM, deduped + bootstrap; supersedes §7 where they conflict)

1. **`pead` stays let-run — now bootstrap-confirmed.** Every quick-win variant is significantly
   mean-worse on LARGE (paired CIs exclude 0), let-run also wins the portfolio sim at realistic
   slot counts, and it holds up risk-off. Done question.
2. **The `pead` edge concentrates in gaps ≥10%** (LARGE: +0.31% at 7-10% gap vs +1.94% at 10-15%,
   +4.00% at 15%+). Don't hard-gate, but when capital or DD slots bind, **rank pead candidates by
   gap size** — a 7% gap barely pays. (`GAP_THRESHOLD_PCT=5` as a *label* stays fine.)
3. **`disco` recommendation (FINAL, after the §6d disco-entry test): harvest, don't let-run.**
   On the entry style disco actually trades (daily movers, §6d), let-run's mean edge vanishes
   (CIs straddle 0), its win% is 38%, give-back 34% — and at the account level under binding
   capital, tight TP10 compounds ~3x let-run's terminal equity with lower drawdown. Config:
   - `DISCO_TAKE_PROFIT_PCT=10` — the primary harvest (full exit at +10%). TP15 if total-return
     priority beats the quick-win feel (same mean as let-run, best portfolio compounding — §6e).
   - ~~`DISCO_SCALE_OUT_TIERS=8:0.5` as a co-equal alternative~~ **DEMOTED by §6e:** scale-outs
     are dominated under binding capital (remnant holds the slot the full hold — portfolio 1.7-2.7x
     vs TP's 5-13x). Only worth it if the owner can't stomach fully exiting winners. If used: the
     trim belongs at +8-10%, never +5%.
   - Whole-share rule already settled (owner, 2026-06-10): trim = round(frac×shares), min 1, skip
     if 0 — modeled in `--mode lot`, no erosion at $310 lots.
4. **The risk-off caveat assigns by BOOK, not by exit:** on PEAD entries a tight TP dies risk-off
   (keep pead let-run always); on movers entries TP10 is the best risk-off policy (§6d) — so the
   disco harvest needs no regime switch. The one regime rule the data does support: **mega-cap
   movers in a downtrend lose under every exit** — if the agent is buying LARGE-cap movers while
   SPY<50dMA, the entry itself is the mistake (`REGIME_ENTRY_GATE` stays the owner's call; this is
   a DD-prompt heuristic, not a gate).
5. **Protection layer settled: stop12 / softcut8 / be12 is right under every exit family tested.**
   No interaction with scale-out. Stop wider than 12 only "wins" via the survivorship trap.
   Breakeven rung is free; trigger anywhere ≥12 is equivalent.
6. **Hold horizon: keep MAX_HOLD_DAYS=21 (~15 trading days); 20d is mildly better for let-run.**
   Quick-win does NOT prefer shorter max-holds — the trim happens early on its own; the remnant
   needs the runway.
7. **Costs and whole-share rounding are non-issues** (rankings stable through 40bps/leg; $310 lots
   match exact fractions).
8. **Honesty flags:** MIDCAP 2025 was the sample's worst year and 2026 YTD is flat — the disco
   proxy's recent tape is hostile; all-history means overstate the present. And every disco number
   still rides a survivorship-biased universe: direction trustworthy, magnitude not. Paper-first
   (≥30 round-trips, `pnl_report.py --by-book`) before arming any DISCO_* change stands.

---

## 8. Caveats & limitations (read before trusting any number)

- **Daily bars, not intraday.** Trailing ratchets off prior-day highs; intraday whipsaw is not fully
  captured. Live runs a 5-min cadence.
- **The fine gaps are inside the noise floor.** With sharpe ≈0.14, per-trade SD ≈10–11%; over ~361
  trades the SE on the mean is ≈±0.5%. So `+1.48` vs `+1.43` is a coin-flip; trust **big** moves
  (the trail-width gradient, the median/gaveback shifts), not third-decimal rankings. Comparisons are
  *paired* (same entries), which helps, but don't over-read.
- **MIDCAP is survivorship + recency biased** → its mean is optimistic; its *direction* (let-run median
  negative, quick-win median positive) is the trustworthy part.
- **No real disco-universe data exists.** MIDCAP is the closest proxy. The disco profile MUST be earned
  forward in paper (≥30 round-trips, `pnl_report.py --by-book`), per the two-book disarm rule.
- **Scale-out backtest assumes a hard stop on the remnant.** Live keeps that true only by using
  whole-share trims (so the remnant ≥1 whole share). Honored by the round-to-whole-share rule.
- **Entry is held fixed** at gap7/vol2. A different entry could change the best exit.

---

## 9. How to run

```bash
# Exit-policy sweep (stop/trail/be/softcut/tp), one universe:
python3 scripts/backtest_exit_policy.py --universe LARGE --gap 7 --hold 15
python3 scripts/backtest_exit_policy.py --universe BOTH --gap 7 --hold 20

# Quick-win / scale-out sweep (adds p10 + gaveback; runs LARGE and MIDCAP):
python3 scripts/backtest_quickwin.py --gap 7 --hold 15
python3 scripts/backtest_quickwin.py --dedupe --boot 2000          # live-realistic entries + CIs

# The full backlog campaign (all grids/sensitivities; see MODES in the script):
python3 scripts/backtest_sweeps.py --mode all
python3 scripts/backtest_sweeps.py --mode tiergain --boot 2000     # one axis, with CIs
python3 scripts/backtest_sweeps.py --mode disco --boot 2000        # the risky-mode (movers) cohort
python3 scripts/backtest_sweeps.py --mode portfolio --entry movers # account-level, disco stream

# --refresh re-pulls history from Cboe (else uses data/history cache).
```

## 10. How to extend (the scale-out part)

To add policies, edit the `POLICIES` list in `backtest_quickwin.py` (`main()`):
```python
("my-policy-label", P(tp=12, trail=15, activate=20, tiers=[(6, 0.5)])),
```
`P(**kw)` overlays onto the shared base `{stop:12, softcut:8, be:12}`. `tiers` is a list of
`(gain_pct, fraction_of_original)`, processed ascending. To add a universe, append a hardcoded list to
`backtest_gap_drift.py` and add it to the loop in `backtest_quickwin.main()`. New metrics go in
`evalpol`. Keep `simulate` in sync with `live_execute.trail_stop_price` if live rungs change.

## 11. Backlog — status after the 2026-06-10 PM campaign

DONE (all in `backtest_sweeps.py`, results in §6c): ~~1. tier-gain grid~~, ~~2. trim-fraction grid~~,
~~3. two-tier ladders~~, ~~4. hold sensitivity~~ (quick-win does NOT want shorter max-holds),
~~5. entry sensitivity~~ (exit ranking robust to entry), ~~6. cost sensitivity~~ (survives 40bps),
~~7. whole-share rounding~~ (non-issue), ~~8. regime conditioning~~ (via keyless Cboe SPY 50dMA —
better than the short `market_conditions.jsonl` window), ~~10. bootstrap CIs~~ (`--boot`, paired).
Plus unplanned: protection-layer grid, entry-quality (gap-size/close-strength) split, year-by-year
stability, capital-constrained portfolio sim (§6c — the ranking-flipper), and the **disco-entry
movers cohort** (§6d, `--mode disco` / `--entry movers` — the risky-mode test that settled the
disco exit question).

Remaining:
1. **A bigger / less-biased MIDCAP+SMALL universe** (the honest fix for the disco-proxy gap): add
   delisted names if any keyless source can be found, or at least widen the current list and note the
   residual bias.
2. **Forward validation in paper** — ≥30 disco round-trips with `DISCO_SCALE_OUT_TIERS=8:0.5` vs the
   ledger's let-run history (`pnl_report.py --by-book`). The backtest question is settled; the
   universe question only paper can answer.
3. **Slot-occupancy telemetry:** log how often disco entries are deferred for lack of settled
   cash/slots — decides between the harvest (§7b.4) and let-run ends of the spectrum with live data.

Record every run's command + date + table here in §6 so this stays a living log.
