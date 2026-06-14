# Kalshi "near-certain favorite" — FINDINGS (Gate 1 / H2 = FAIL)

> **STATUS 2026-06-14 — ARCHIVED NO-EDGE RESULT.** Research artifact, no capital.
> Tests the owner idea: *buy the favorite side (YES/NO) in markets ending soon that
> trade ≥0.90, hold to settlement.* Verdict: **systematically negative on Kalshi.**

## Method
- Tool: `scripts/kalshi_pull.py` (public, read-only Kalshi REST API).
- Universe: discovered the liquid "ending soon" set — recurring short-dated series
  (daily/weekly/hourly/15min): 510 of 10,845 series; the tradable feed is ~99.99%
  auto-generated MVE parlays, excluded. Ranked by aggregate open interest; swept the
  **top 50** by OI.
- For each settled market, reconstructed the price **24h before close** from
  candlesticks and bought the favorite (ask ≥ 0.90) — **at the ask** (real buy cost,
  not mid) — held to settlement.
- Net of Kalshi's published quadratic fee (`0.07·p·(1−p)` per contract, smallest at
  the extremes).

## Result (n = 1,655 qualifying bets; 5,421 markets scanned, 4,714 with candle history)
| Metric | Value |
|---|---|
| Mean entry (implied p) | 0.960 |
| Realized favorite win-rate | **0.927** (1535/1655) |
| Calibration edge (real − implied) | **−0.033** → favorites **OVERpriced** |
| Mean net P&L / contract | −0.0356 |
| Mean net return on stake | **−3.78%** per ~1-day hold |
| Naive t-stat (optimistic, independence) | **−5.66** |
| Drop top-1 / top-5 winners | −3.79% / −3.82% |
| Loss rate | 120/1655 = 7.3%, worst −100.7% |

By price bucket: `[0.90–0.95)` n=493 win=0.872 net **−6.13%**; `[0.95–1.00)` n=1162
win=0.951 net **−2.78%**. Both negative.

## Interpretation
1. **No edge — the sign is wrong.** Favorites are priced ~3.3¢ *richer* than they
   resolve. The favorite-*underpricing* reported in some Polymarket studies does **not**
   appear on Kalshi at this horizon; here it reverses. You buy at the ask and eat the
   spread on top of an already-rich mid.
2. **Not a survivorship/steamroller artifact.** Unlike the 22-bet pilot (where one
   −100% upset drove the loss), dropping the top winners barely moves the result and the
   loss rate is a calm 7.3%. The negative return is **broad and systematic**, robust to
   the drop-top-N tripwire from `xsection-momentum-is-survivorship`.
3. **Fees are not the culprit** — they're tiny at the extremes; the loss is the
   ask/spread vs. realized-frequency gap.

## Caveats (don't over-read)
- Candle **ask** captures spread but not depth/partial fills (true large-size fills
  could be worse, not better).
- Single horizon (24h); ~1% of series carry extra maker fees (ignored).
- t-stat assumes independent bets; resolutions cluster, so significance is *overstated*
  — but the point estimate is so negative this doesn't rescue the idea.

## Decision
**Archive. Do not build.** Gate 1 fails decisively. Possible (low-priority) follow-ups
only if someone wants to keep digging — none expected to flip the sign:
- Other horizons (6h / 48h) from the cached series list (discovery already built).
- The *opposite* trade (fade the favorite / buy the longshot) is implied profitable by
  this data **on paper**, but it's the thin, capacity-constrained tail and almost
  certainly un-tradable after real depth — not pursued.

Reproduce: `python3 prediction-markets/scripts/kalshi_pull.py --top 50 --horizon-hours 24`
