# Signal Backtests — does the entry signal actually have an edge?

**Date:** 2026-06-04
**Scripts:** [`scripts/backtest_signal.py`](../scripts/backtest_signal.py),
[`scripts/backtest_xsection.py`](../scripts/backtest_xsection.py),
[`scripts/backtest_gap_drift.py`](../scripts/backtest_gap_drift.py)
**Data:** keyless daily OHLCV from Cboe's CDN (`cdn.cboe.com/.../charts/historical/{sym}.json`),
split-adjusted, back to 2004 — the same provider `dd_probe.py` uses. Cached under
`data/backtest/history/` (gitignored).

## Why we ran this

The live engine has a careful execution stack — Stage-2 DD, marketable-limit fills, slippage
modelling, resting stops, caps. **None of that answers whether the entry *signal* makes money.**
Every guardrail protects against blowing up; none creates an edge. Before tuning slippage or stops,
we needed one number: the raw expectancy of the signal itself, net of costs. So we backtested it
with the LLM removed.

The headline result: **the engine's intraday signal has no edge at the horizon it trades**, but two
*different*, multi-day constructions do — cross-sectional medium-term momentum and catalyst
gap-drift (PEAD) — each statistically defensible, each gated by a survivorship caveat. The three
scripts test those claims in turn.

---

## Backtest 1 — the raw intraday signal (`backtest_signal.py`)

### What it models

The live screen (`tick_context.py`) enters when a name is up ≥ `SIGNAL_THRESHOLD_PCT` on the day
**and** ≥ `REL_STRENGTH_PCT` above SPY, then manages a `STOP_LOSS_PCT` / `TAKE_PROFIT_PCT` exit.
This script replays exactly that, on daily bars:

- **Entry:** on day *D*, if the name's close-to-close move ≥ threshold **and** (move − SPY move) ≥
  rel-strength, buy at *D*'s close.
- **Exit:** forward-walk up to `--hold` days with a **gap-aware** stop/TP. An open beyond a level
  fills at the open (worse than the level) — this captures the gap risk the engine's ~5-min
  synthetic stop cannot cover. Stop is checked before TP within a bar (conservative).
- **Cost:** `--cost-bps` per side, round-tripped (default 10 bps/side = 20 bps round trip, matching
  `.env`'s `SLIPPAGE_BPS=10`).
- **Edge test:** alongside the traded P&L, it compares the forward *H*-day return on **signal days**
  vs **all days** in the same universe. If signal-day returns ≤ unconditional, the entry adds
  nothing — the exit geometry, not the signal, is doing the work.

### What it deliberately does *not* model

- The literal ~5-min intraday entry/exit (daily bars → minimum hold is overnight).
- The LLM catalyst filter (off on purpose — we want the **raw** signal).
- Same-day churn / EOD flatten timing.

### Results (3% move, 3% vs SPY, −4%/+12% exits, 2010→now, 60 large-caps)

| Hold | Win rate | Avg trade **net** | Conditional edge vs any-day | Regime |
|-----:|---------:|------------------:|----------------------------:|--------|
| **1 day** | 44.4% | **−0.19%** | −0.07% | **reversal** |
| 2 day | 48.2% | −0.07% | +0.02% | noise |
| 3 day | 47.8% | −0.01% | +0.02% | noise |
| 5 day | 45.0% | +0.06% | +0.12% | weak momentum |
| 10 day | 41.1% | +0.16% | +0.43% | real momentum |

And at the engine's real ~1-day horizon, **being more selective makes it worse** — the biggest
gainers give back the most:

| Threshold @ 1-day hold | Conditional edge | Avg trade net |
|-----------------------:|-----------------:|--------------:|
| 3% move | −0.07% | −0.19% |
| 5% move | −0.29% | −0.34% |
| 7% move | −0.64% | −0.69% |

### Conclusion 1

The signal is **break-even gross, negative net of costs** at the default 3-day hold (profit factor
0.99), and outright **anti-predictive (short-term reversal) at 1 day** — exactly where the engine
operates (EOD flatten, 120-min max-hold, ~5-min synthetic stop). A modest momentum edge only appears
at 5–10 day holds the engine is architecturally built to avoid. **Tuning slippage/stops cannot fix
this** — at 0 bps cost the 1-day signal is still anti-predictive, so fills are not the bottleneck.
The *entry* is.

---

## Backtest 2 — cross-sectional momentum (`backtest_xsection.py`)

Backtest 1 pointed at the one horizon with edge (5–10+ days). This tests the construction that edge
classically lives in: **cross-sectional momentum**, not absolute single-name pops.

### What it models

A portfolio (compounded equity curve), rebalanced periodically:

- **Rank** the whole universe by trailing-`--lookback`-day return, **skipping the most recent
  `--skip` days** (the standard 12-1 construction — skip the last month precisely to dodge the
  short-term reversal Backtest 1 measured).
- **Buy** the top `--topk`, equal weight, hold to the next rebalance (`--rebalance` days).
- **Cost** charged on turnover (names rotated in since last rebalance, round-tripped).
- **Benchmarks:** equal-weight of the *same* universe (shares the survivorship bias — this is the
  benchmark that matters) and SPY (external anchor).

### Results

Default 12-1 (252d lookback, top 10, monthly) is positive but not significant (+3.2%/yr over EW,
t = 1.15). The **sweep** shows the edge is real and concentrated:

```
 lookback  topk |  mom CAGR  EW CAGR  spread/yr  t-stat
       63     5 |     28.3%    16.0%     +12.3%    2.99
      126     5 |     28.5%    16.4%     +12.1%    3.05   <- best
      189     5 |     25.5%    16.2%      +9.3%    2.34
      252     5 |     23.0%    16.1%      +6.9%    1.71
      126    10 |     22.6%    16.4%      +6.3%    2.14
      126    15 |     18.0%    16.4%      +1.6%    0.80
```

The edge is strongest and significant (t ≈ 2.3–3.1) at **top-5, 3–6 month lookback**, and decays
cleanly as you dilute (top-15) or lengthen the window — the gradient of a real factor, not a fluke.
Best config (126d / top-5 / monthly) over ~21.8y: **+12.1%/yr spread over equal-weight, t = 3.05**,
but with a **61% max drawdown**.

### Conclusion 2

Cross-sectional momentum shows a **statistically defensible edge** over equal-weighting the same
universe — the opposite verdict from the intraday signal. This is the construction worth building a
live engine around: monthly rotation, overnight holds, concentrated top-K, with **mandatory resting
broker stops** (gaps become the primary risk once you hold overnight).

---

## Backtest 3 — catalyst gap-drift / PEAD (`backtest_gap_drift.py`)

The "hotter edge" hypothesis: an agent's real advantage is **breadth + drift**, not speed. A big
**overnight gap on a volume spike** is a keyless proxy for a genuine catalyst (earnings beat,
guidance raise, M&A). Post-earnings-announcement drift (PEAD) says under-covered names *under-react*
and drift over the following days/weeks — a multi-day edge that dodges the 1-day reversal that killed
the intraday signal. We measure drift from the **gap day's close forward** (textbook PEAD;
conservative — it doesn't claim the gap-day pop), on two universes: the same 60 large-caps and a
40-name higher-beta mid-cap basket.

### Data hazard found and fixed

Cboe back-fills tickers with **pre-listing junk** — SPAC shells show ~$0.01 ghost bars years before
the real IPO (e.g. TWLO at $0.01 in 2016, DKNG at $0.01 in 2014), producing 100,000%+ ghost
"returns" that detonate the mean. `clean_bars()` strips this: drop sub-$2 bars, then start each
series *after* the last >80% overnight jump (the boundary between ghost data and real trading). A
>300% `hold`-day move is dropped as a residual guard. Medians are reported alongside means because
the (cleaned) small-cap distribution is still heavily right-skewed.

### Results — the edge is real, scales with surprise, and accumulates over weeks

Sweep of forward-return edge (gap days − all days) and t-stat, vol-mult ≥ 2×:

```
[LARGE]   gap%  hold |   edge      t      n          [MIDCAP]  gap%  hold |   edge      t      n
             5    10 |  +0.93%   2.18   655                       5    10 |  +0.50%   0.82   848
             5    20 |  +1.21%   2.38   652                       5    20 |  +1.97%   2.01   842
             7    20 |  +2.07%   2.64   360                       7    20 |  +2.42%   1.94   595
            10    10 |  +3.00%   2.24   165                      10    20 |  +3.24%   1.96   388
            10    20 |  +4.39%   3.10   163                      15    10 |  +2.68%   2.26   192
            15    20 |  +7.88%   2.25    47                      15    20 |  +4.47%   1.92   191
```

Two clean PEAD signatures: the edge grows **monotonically with gap size** (bigger surprise → bigger
drift) and **with horizon** (drift accumulates over 10–20 days, not intraday). This is the first
signal in the whole study with **consistent t > 2** across multiple parameter cells.

### The surprise: it's CLEANER in large caps, not mid-caps

The naive "less efficient = bigger edge" intuition did **not** replicate. Large-cap gap-drift carries
the higher t-stats; the mid-cap version is weaker and statistically marginal (mostly t < 2). The
distribution tells the story — at 5% gap / 10d hold, the **median mid-cap gap trade is −8.3%** (most
hit the −8% stop; 38% win rate). The positive mid-cap *mean* is a lottery carried by a few huge
winners — and that right tail is exactly where survivorship + recency bias lives, so it's the least
trustworthy number here. Likely cause: large-cap gaps are almost always *real* catalysts (clean
signal); mid-cap gaps mix catalysts with pumps, sympathy moves, and noise (dirty signal).

### Conclusion 3 — and where the agent earns its keep

There is a **real, statistically defensible catalyst-drift edge**, strongest in large caps, that
behaves like textbook PEAD. Crucially it is a **multi-day-to-weeks** edge — fundamentally
incompatible with the engine's intraday-flatten design, confirming yet again that the *architecture*,
not the fills, is the constraint. The dirty mid-cap signal is exactly where an agent's breadth +
judgment could add value the dumb gap+volume filter can't: read the actual filing across thousands of
names, **confirm the catalyst is durable and filter the pumps** that wreck the mid-cap median. That
filtering is load-bearing, not optional — naive mid-cap execution loses.

## The caveat that gates everything: survivorship bias

Both backtests use a **fixed universe of 60 of today's liquid large-caps**. These are *survivors* —
they were large enough to still matter in 2026 — so the universe is biased upward, and **momentum is
the strategy most inflated by that bias**: it concentrates capital into whichever names ran up, and
we already know these ran up. Equal-weight shares the bias (which is why it's the benchmark), but
momentum *exploits* it harder, so a meaningful chunk of the +12% is the universe, not the signal.

**The t-stat is honest within this universe; it cannot correct for the universe being the rigged
part.** Status: *promising enough to validate properly, nowhere near "trade real money on these
numbers."*

The real next gate is a **point-in-time, survivorship-free universe** (constituents as they existed
each year, including names later acquired or delisted). If +12% survives that, there's a signal; if
it collapses toward 0, it was always the bias. PIT constituent history is **not** on the Cboe CDN, so
this needs either a larger live-screened universe (reduces, doesn't kill, the bias) or a sourced
constituent dataset.

---

## Validation (2026-06-05): the cross-sectional momentum edge WAS the bias

Two cheap, keyless tests — run *before* sourcing any delisted-price data — answer the gate above.
**Cross-sectional single-name momentum collapses to zero out-of-universe.** (Harness: `backtest_xsection.py`
gained `--universe` / `--drop-top`, and `run()` now excludes SPY from the tradable basket — it was
previously ranked/EW'd as a name. That fix nudges the pinned top-5/126d cell to **+11.9%/yr, t=3.01,
61.0% MaxDD**; same story, cleaner number.)

### Test A — drop the 10 best full-span performers (`--drop-top 10`)
Real momentum skill rotates among whatever is strong; it shouldn't hinge on a few names. It does.
Removing the 10 biggest 22-year winners — **AVGO (+4,792,200%), NVDA, AAPL, NFLX, TSLA, BKNG, GOOGL,
AMZN, MA, CRM** — from the same 50-name remainder:

| | CAGR | MaxDD | spread vs EW | t-stat |
|---|---:|---:|---:|---:|
| Full 60-name universe | 28.5% | 61.0% | **+11.9%/yr** | **+3.01** |
| Drop best 10 → 50 names | 12.6% | 66.3% | **+0.0%/yr** | **+0.30** |

The spread goes to **exactly zero**. Momentum had no timing skill — it "worked" only by being
mechanically overweight names that rose monotonically. (Dropping by end-of-sample return is an
*attribution* test, not a tradable rule — it measures how concentrated the "edge" is in known winners.)

### Test B — sector-ETF momentum, a survivorship-FREE universe
The 9 SPDR sectors (XLK XLF XLE XLV XLY XLP XLI XLB XLU) **never delist** — no universe bias to exploit.
Top-3, monthly, 5 bps/side:

| lookback | topk | spread vs EW | t-stat |
|---:|---:|---:|---:|
| 252d | 3 | +0.5% | +0.26 |
| 126d | 3 | −0.5% | −0.32 |
| 63d | 3 | −0.8% | −0.40 |
| 252d | 2 | +1.2% | +0.58 |

**No edge at any window or concentration.** In a universe that can't be survivorship-inflated,
momentum ≈ equal-weight.

### Conclusion 4 (supersedes Conclusion 2 for live deployment)
**Do not build the live engine around cross-sectional single-name momentum.** The +12% was the
universe, not the signal — Tests A and B are two independent confirmations. The planned sleeve upgrades
(residualize / vol-scale / absolute-gate) would have polished a number that isn't there; this **cancels
the swing-momentum rewrite.** The survivor is **Backtest 3 (catalyst gap-drift / PEAD)** above — the
only construction here with a real t-stat on the *trustworthy* large-cap control (t up to 3.10 at
10–20d holds) and the most agent-native. The catalyst-drift thread is the one to pull next.

---

## Backtest 4 — does gap-drift survive as a TRADED book? (`backtest_catalyst_book.py`, 2026-06-05)

Backtest 3's t-stats are an *event study* (per-event forward returns) — not tradable. This turns the
same signal into the owner-locked risky-sleeve book (`strategies/catalyst-drift-v1-plan.md`):
concentrated, whole-share, multi-day hold, gap-aware + optional trailing stop, shared cash account,
compounded. ~22y, LARGE+MIDCAP (100 survivor names), vs SPY buy&hold.

| Config (BOTH baskets) | CAGR | MaxDD | Sharpe | avg trade | avg invested |
|---|---:|---:|---:|---:|---:|
| 6×15%, gap7/hold15 | 5.6% | 38.5% | 0.43 | +1.62% | **21%** |
| 10×18%, gap5/hold20 | 9.2% | 57.7% | 0.53 | +1.95% | **38%** |
| + trailing stop 12% | −0.3% | 47.2% | 0.05 | +0.35% | 35% |
| SPY buy&hold | 8.9% | 56.5% | 0.55 | — | 100% |

**Three findings that matter more than the headline:**
1. **Per-trade expectancy is genuinely positive** (+1.4% to +1.95%/trade, net of 15bps/side, *unfiltered*,
   win 42–51%). The signal works at the trade level.
2. **The book is capital-starved, not edge-starved.** At 100 names a gap≥7% catalyst is rare, so the
   book sits **~80–90% in cash** — that idle capital, not a bad signal, is what drags CAGR. Forcing
   deployment (lower gap, more/longer positions) lifts CAGR toward SPY but **dilutes signal quality and
   pushes MaxDD to SPY-like levels**. Best risk-adjusted cells (Sharpe 0.65–0.69) are gap≥10–15% /
   15–20d — high-quality but *rare* (2–8% invested). It's a selective, mostly-cash, concentrated edge.
3. **Trailing stops destroy it** (Sharpe 0.05). The edge *is* the multi-day drift; a tight trail exits
   before it plays out. Exits must be time-based (~15–20d) + a hard stop, **not** a tight trail.

**The three levers the backtest can't model — all favorable, all the agent's actual job:**
(a) **market-wide breadth** — live discovery ranges over the *whole* market's gappers each day, not 100
fixed names, so the book can stay deployed on *high-quality* (gap≥10%) signals without lowering the bar;
(b) the **catalyst-confirmation / pump-filter** — the one thing that could lift the 42–49% unfiltered win
rate; (c) **survivorship/recency** drags MIDCAP down here (pessimistic).

### Conclusion 5 — plausible, not proven
Unfiltered and capped at 100 names, the catalyst book **≈ SPY risk-adjusted in-sample** — a SPY-tracker
with extra steps. The case for it being a *real edge* rests entirely on the three unmodeled levers above.
That is a legitimate live thesis (breadth + pump-filter are exactly what an agent adds), but it is **not
backtest-proven** — so: build it, **paper-validate that the agent's filter lifts win rate**, size small,
and don't bet big real money on the in-sample numbers. Go in knowing the floor is "≈SPY with concentrated
drawdowns," and the upside is unproven.

---

---

## Backtest 5 — does the Tier-1 soft-cut earn its keep? (`backtest_exit_policy.py --universe BOTH`, 2026-06-09)

The live audit (docs/remediation-plan-2026-06-09.md P1) found the engine's Tier-1 protective sell
(`hold_risk.py`: soft-cut at −4% "still falling" + critical-band at ~65%-of-the-way-to-stop) was
**never backtested** — and on 2026-06-09 it fired 14× in one session, realizing −4..−9% on day-0/1 of
15–21-day drift theses. `simulate()` now models both layers (close-of-down-day soft-cut; close-based
critical exit), so the policy is priced instead of vibes. On top of the current live config
(stop12 / tp40 / trail15@20), gap≥7% / vol≥2× / 15d hold, 15 bps/side:

| Policy (LARGE, n=361) | mean | median | win% | sharpe |
|---|---:|---:|---:|---:|
| TIME-ONLY (drift ceiling) | +1.50% | +1.12% | 55% | 0.117 |
| **LIVE + softcut8** | **+1.48%** | +0.98% | 53% | **0.141** |
| LIVE config (no Tier-1) | +1.43% | +0.98% | 53% | 0.134 |
| LIVE + crit65 | +1.40% | +0.94% | 53% | 0.133 |
| LIVE + softcut6 | +1.33% | +0.74% | 51% | 0.129 |
| LIVE + softcut4 (what was running) | **+1.04%** | **−1.82%** | **45%** | 0.104 |

MIDCAP (n=601, biased but directionally consistent): softcut8 +1.88%/0.118 vs plain +1.70%/0.103;
softcut4 +1.17%/win 36%.

**Verdict:**
1. **The −4% soft-cut was destroying ~0.4%/trade** — about a third of the entire LARGE edge — and
   cut the win rate from 53% to 45%. Exactly the "tighter stop clips the drift on noise" failure
   Backtest 4's trailing-stop result predicted. It is strictly worse than having no Tier-1 at all.
2. **A DEEP soft-cut (8%) is the one Tier-1 variant that beats the plain config on mean AND sharpe
   in both universes** — it salvages the worst losers a few days before the −12% stop without
   clipping normal drawdown-then-drift. Adopted: `SOFT_CUT_PCT=8.0`.
3. **The critical-band auto-sell fails the bar** (mean below plain, sharpe a wash) — disabled as a
   sell trigger (`HOLD_RISK_CRIT_SELL=0`); the band still drives the Tier-2 re-DD cadence.

Caveats: daily-bar proxy (close-of-down-day ≈ "still falling"; the live monitor reacts intraday);
the conviction/runner exemption isn't modeled; same survivorship limits as Backtests 3–4.

## How to reproduce

```bash
# Raw intraday signal — defaults mirror .env (3% / 3% / -4% / +12%, 20bps round trip)
python3 scripts/backtest_signal.py
python3 scripts/backtest_signal.py --hold 1 --threshold 5 --rel 5   # selectivity at 1d
python3 scripts/backtest_signal.py --refresh                        # re-pull history

# Cross-sectional momentum
python3 scripts/backtest_xsection.py                                # 12-1, top 10, monthly
python3 scripts/backtest_xsection.py --lookback 126 --topk 5        # pinned cell, full stats
python3 scripts/backtest_xsection.py --sweep                        # lookback x topk grid
python3 scripts/backtest_xsection.py --lookback 126 --topk 5 --drop-top 10   # Test A: survivorship floor (-> t=0.30)
python3 scripts/backtest_xsection.py --universe XLK,XLF,XLE,XLV,XLY,XLP,XLI,XLB,XLU --topk 3 --cost-bps 5  # Test B: sector ETFs

# Catalyst gap-drift (PEAD) — event study
python3 scripts/backtest_gap_drift.py                               # both universes, 5% gap, 10d
python3 scripts/backtest_gap_drift.py --gap 10 --hold 20            # bigger surprise, longer drift
python3 scripts/backtest_gap_drift.py --sweep                       # gap x hold grid, both universes

# Catalyst-drift as a TRADED book (portfolio sim)
python3 scripts/backtest_catalyst_book.py --universe both           # default 6x15%, gap7/hold15
python3 scripts/backtest_catalyst_book.py --universe both --gap 5 --hold 20 --max-pos 10 --pos-pct 0.18
python3 scripts/backtest_catalyst_book.py --sweep --universe both   # gap x hold grid w/ CAGR/MaxDD/Sharpe

# Exit-policy sweep incl. the Tier-1 soft-cut/critical layer (Backtest 5)
python3 scripts/backtest_exit_policy.py --universe BOTH
```

Both cache history under `data/backtest/history/` on first run; subsequent runs are instant.

## Takeaways

1. **Don't tune the intraday algo/slippage** — the entry signal is anti-predictive at its own
   horizon; fills are not the bottleneck.
2. **The intraday absolute-pop strategy operates in the reversal zone** and caps out before the
   momentum zone. Its premise ("bigger move = buy more") is backwards intraday.
3. **Only ONE construction survives the survivorship test: catalyst gap-drift / PEAD** (t up to 3.1,
   strongest in large caps, scales with gap size and horizon). Cross-sectional single-name momentum
   *looked* like an edge (t≈3) but **collapsed to zero** once the 10 best winners were dropped
   (t=0.30) and on a never-delisting sector universe (|t|<0.6) — see Validation above. It was the
   universe, not the signal. Gap-drift is also the most agentic-friendly — it rewards reading
   catalysts across a wide universe and filtering pumps.
4. **The engine's architecture is the real blocker.** The surviving edge lives at a 10–20 day
   horizon; the intraday-flatten / 120-min-max-hold / 5-min-synthetic-stop design is built to avoid
   exactly the horizon that pays. A profitable engine needs overnight holds and resting broker stops.
5. **The data gate is half-closed already.** Two keyless robustness tests (drop-best-10, sector ETFs)
   killed the momentum thesis *without* needing a delisted-price dump — the cheap test pre-empted the
   expensive one. For gap-drift, the load-bearing unknowns remain the agent's live catalyst-confirmation
   filter (can't be backtested here) and an out-of-universe / leakage-clean re-run of the large-cap drift.
