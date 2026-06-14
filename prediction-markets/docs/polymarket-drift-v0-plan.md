# Polymarket Drift — v0 Research-Design Note (EXPLORATORY, NOT LIVE)

> **STATUS 2026-06-13 — RESEARCH ARTIFACT ONLY. Nothing here is wired to the live engine.**
> This note records the design for testing whether our equity edge (post-event drift /
> underreaction) carries over to prediction markets. It is **out of the current operating
> scope** defined in `CLAUDE.md`: that mandate is the single Robinhood **cash equities**
> account (`your_account_number`), equities-only beta, no new rails. Polymarket is a separate
> crypto-wallet venue with its own legal/operational/funding surface. **Do not connect a
> wallet, fund anything, or place a Polymarket order off this note.** Treat it as a
> pre-registered research plan to be approved (or killed) before any capital or infra work.

Origin: review of 5 fintwit posts (2026-06-13). Post #5 (@0xLin_i) was a referral-farming
scam ("$0.90 → $408k in 48h with one Claude prompt"), but the *underlying concept* —
prediction-market mispricing — is real. This note separates the two cleanly.

---

## 1. What we are NOT doing — and why

The viral strategy is **latency arbitrage**: Polymarket's 15-min BTC/ETH/SOL "up-or-down"
contracts lagged Binance/Coinbase spot by ~30–90s; bots bought the stale side. Documented and
once profitable (~$40M extracted Apr-2024→Apr-2025; the real "$300→$400k/month" wallet).

We are **explicitly not pursuing this**, on every axis:
- **Wrong edge type.** It's an execution-speed race (colocated VPS, ~1,000 orders/sec). Our
  live tick is a ~4-min Python loop through an MCP relay on consumer infra. We would be the
  *slow money being arbitraged*, not the arbitrageur.
- **Closing window.** Polymarket shipped a **dynamic taker fee** specifically to neutralize
  latency arb on short crypto markets. The free-money phase is being fee'd out.
- **Wrong rails / scope.** Crypto wallet, not RH cash equities. Out of `CLAUDE.md` mandate.

This matches our standing lesson that **our edge is not speed** — intraday is anti-predictive
for us (see memory `signal-has-no-intraday-edge`); the edge lives in multi-day drift.

---

## 2. The actual hypothesis — our drift edge, ported

Our equity thesis (`catalyst-drift-v1-plan.md`): **the market underreacts to a catalyst and
price drifts toward fair value over ~5–10 days** (PEAD / gap-drift). The research question is
whether the *same underreaction behavior* shows up in prediction-market prices — a
**low-frequency, statistical** edge, not a latency one.

Published evidence that non-latency inefficiencies exist (to be re-verified, not trusted):
- **Political markets are chronically *underconfident*** — prices compressed toward 50%, i.e.
  they under-move vs. true probability. (arXiv 2602.19520; SSRN Reichenbach & Walther 2026)
- **Favorite-longshot *reversal* on Polymarket** — longshots overpriced (negative realized
  returns), favorites underpriced (positive) → tilt: buy high-prob favorites, fade tail
  longshots. (QuantPedia, "Systematic Edges in Prediction Markets")
- **Post-news under/over-reaction with measurable mispricing *duration*** — the direct
  PEAD analog: after a catalyst moves true probability, does the contract drift toward the new
  level over hours-days?

### Pre-registered hypotheses (frozen before any backtest is run)

- **H1 — Post-news drift.** After a discrete news event that moves a market's fair probability,
  the contract price **underreacts** and continues drifting in the news direction over the
  following H hours/days. Test horizons H ∈ {6h, 24h, 72h, to-resolution}.
  - *Falsifier:* drift t-stat < 2 after transaction costs (current dynamic taker fee), OR the
    effect is fully absorbed within one tick (i.e. it's latency, not drift, and not ours).
- **H2 — Favorite underpricing / longshot overpricing.** Bucketed by entry price, high-prob
  contracts (≥0.80) earn positive cost-adjusted returns; low-prob (≤0.15) earn negative.
  - *Falsifier:* monotonic relationship absent, OR sign flips once delisted/void markets are
    included (survivorship — see §4).
- **H3 — Political underconfidence.** Prices systematically compressed toward 0.5 vs. realized
  frequency; betting *with* the favored side at price p captures the gap.
  - *Falsifier:* calibration curve within CI of the 45° line after binning by liquidity/horizon.

All three must clear **after fees + realistic fill (depth-aware)**, not on mid-price.

---

## 3. Data & method (mirrors how we backtest equities)

- **History source:** Polymarket on-chain CLOB trade/resolution history (subgraph / data API)
  — must capture **resolved AND voided/invalid** markets, entry/exit depth, and timestamps of
  the price series, not just final outcome. Cross-check against a public dataset
  (e.g. arXiv "Polymarket-v1 Database", 2606.04217) for coverage sanity.
- **Event tape for H1:** align contract price series to an external news/timestamp feed for the
  same underlying (for crypto markets: spot prints; for political/sports: a dated headline log).
- **Costs:** model the **current dynamic taker fee** + realistic slippage from book depth.
  Mid-price backtests are disqualified.
- **Backtest discipline (non-negotiable, same as the equity book):**
  - Pre-register the hypotheses above; freeze the decision rule before looking at returns.
  - Report t-stats, and the **leave-out stress test** (drop the top-N winners) — our
    survivorship tripwire from `xsection-momentum-is-survivorship`.
  - Forward/out-of-sample split; no parameter mining to a target.

---

## 4. Caveats / known traps (read before getting excited)

- **Survivorship, again.** A "buy favorites" edge is exactly the shape that burned us in equities
  (+12%/t≈3 momentum collapsed to t=0.30 dropping 10 names). Any factor here must include
  voided/delisted markets and pass the leave-out test, or it's an artifact.
- **Capacity is tiny.** Books are thin; the clean documented money was the HFT arb (we can't do).
  The statistical biases are small and now fee-dragged — likely a *learning* exercise more than a
  P&L one.
- **Resolution / oracle risk.** Disputed or mis-resolved markets are a real loss mode with no
  equity analog.
- **Legal / access surface.** US access to Polymarket is its own question — a precondition, not a
  detail. Resolve before any infra spend.
- **New venue ≠ config change.** This would be a *separate book* with separate rails, funding,
  logging, and kill switches — not a flag on the RH engine.

---

## 5. Validation gates (must pass IN ORDER before any capital)

1. **Gate 0 — Legality/rails.** Confirm lawful access + a sanctioned funding/withdrawal path,
   isolated from the RH account. Fail → stop here.
2. **Gate 1 — Edge exists in history.** ≥1 of H1/H2/H3 clears t>2 **after fees + depth-aware
   fills**, and survives the leave-out stress test. Fail → archive as "no edge," done.
3. **Gate 2 — Edge persists forward.** Paper/replay on out-of-sample, post-dynamic-fee data
   (the regime that matters now), ≥N resolved events. Fail → archive.
4. **Gate 3 — Owner sign-off** on a separate, small, hard-capped book with its own daily-loss
   breaker and kill switch, mirroring the live-equity guardrails. Only then, real capital.

Default outcome if any gate fails: **this stays a research artifact.** That is an acceptable,
expected result.

---

## 6. Appendix — related reading (from the same review)

Post #1 (@RuujSs) pointed at two real but distinct bodies of work (the tweet conflated them —
its "not optimization, it's game theory" headline is contradicted by the A-S model it links,
which *is* a single-agent optimization):
- **Avellaneda-Stoikov (2008)** market-making: reservation price (mid skewed against inventory)
  + spread ∝ volatility × time-to-liquidation × risk-aversion. *Not directly applicable — we are
  liquidity takers, not makers.* Loose analogy only to our vol-scaled exit/trail.
- **Kearns (Penn) execution / strategic trading:** optimal VWAP & limit-order execution under
  market impact (Kearns & Nevmyvaka), and "Algorithmic Aspects of Strategic Trading" (Kearns &
  Shi, 2025; no-regret / coarse-correlated equilibria). *More relevant if/when we scale notional*
  — the "censored data" point (we observe only our fills, never the counterfactual) is a genuine
  subtlety for our fill modeling. Low priority at current size (impact negligible).
