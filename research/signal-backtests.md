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

## How to reproduce

```bash
# Raw intraday signal — defaults mirror .env (3% / 3% / -4% / +12%, 20bps round trip)
python3 scripts/backtest_signal.py
python3 scripts/backtest_signal.py --hold 1 --threshold 5 --rel 5   # selectivity at 1d
python3 scripts/backtest_signal.py --refresh                        # re-pull history

# Cross-sectional momentum
python3 scripts/backtest_xsection.py                                # 12-1, top 10, monthly
python3 scripts/backtest_xsection.py --lookback 126 --topk 5        # best config, full stats
python3 scripts/backtest_xsection.py --sweep                        # lookback x topk grid

# Catalyst gap-drift (PEAD)
python3 scripts/backtest_gap_drift.py                               # both universes, 5% gap, 10d
python3 scripts/backtest_gap_drift.py --gap 10 --hold 20            # bigger surprise, longer drift
python3 scripts/backtest_gap_drift.py --sweep                       # gap x hold grid, both universes
```

Both cache history under `data/backtest/history/` on first run; subsequent runs are instant.

## Takeaways

1. **Don't tune the intraday algo/slippage** — the entry signal is anti-predictive at its own
   horizon; fills are not the bottleneck.
2. **The intraday absolute-pop strategy operates in the reversal zone** and caps out before the
   momentum zone. Its premise ("bigger move = buy more") is backwards intraday.
3. **Two constructions show a real edge**, both multi-day: cross-sectional momentum (t≈3, top-5,
   3–6mo lookback) and **catalyst gap-drift / PEAD (t up to 3.1, strongest in large caps, scales with
   gap size and horizon)**. Gap-drift is the most agentic-friendly — it rewards reading catalysts
   across a wide universe and filtering pumps.
4. **The engine's architecture is the real blocker.** Every edge found lives at a multi-day-to-weeks
   horizon; the intraday-flatten / 120-min-max-hold / 5-min-synthetic-stop design is built to avoid
   exactly the horizon that pays. A profitable engine needs overnight holds and resting broker stops.
5. **Next gate is data, not tuning:** the momentum and mid-cap gap numbers are survivorship- (and for
   mid-caps, recency-) inflated. Validate on a survivorship-free / point-in-time universe before
   committing the rewrite. The agent's catalyst-confirmation filter — the load-bearing piece for the
   dirty mid-cap signal — can't be backtested here; it's the live thesis to prove.
