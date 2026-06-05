# Signal Backtests — does the entry signal actually have an edge?

**Date:** 2026-06-04
**Scripts:** [`scripts/backtest_signal.py`](../scripts/backtest_signal.py),
[`scripts/backtest_xsection.py`](../scripts/backtest_xsection.py)
**Data:** keyless daily OHLCV from Cboe's CDN (`cdn.cboe.com/.../charts/historical/{sym}.json`),
split-adjusted, back to 2004 — the same provider `dd_probe.py` uses. Cached under
`data/backtest/history/` (gitignored).

## Why we ran this

The live engine has a careful execution stack — Stage-2 DD, marketable-limit fills, slippage
modelling, resting stops, caps. **None of that answers whether the entry *signal* makes money.**
Every guardrail protects against blowing up; none creates an edge. Before tuning slippage or stops,
we needed one number: the raw expectancy of the signal itself, net of costs. So we backtested it
with the LLM removed.

The headline result: **the engine's intraday signal has no edge at the horizon it trades**, but a
*different* construction — cross-sectional medium-term momentum — does show a statistically
defensible edge (with a large survivorship caveat). The two scripts test those two claims.

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
```

Both cache history under `data/backtest/history/` on first run; subsequent runs are instant.

## Takeaways

1. **Don't tune the intraday algo/slippage** — the entry signal is anti-predictive at its own
   horizon; fills are not the bottleneck.
2. **The intraday absolute-pop strategy operates in the reversal zone** and caps out before the
   momentum zone. Its premise ("bigger move = buy more") is backwards intraday.
3. **Cross-sectional medium-term momentum is the only construction here with a real edge** — but the
   number is survivorship-inflated and unproven out-of-universe.
4. **Next gate is data, not tuning:** validate on a survivorship-free universe before committing the
   engine rewrite.
