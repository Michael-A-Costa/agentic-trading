# Kalshi Passive Market-Making — v1 Research-Design Note (EXPLORATORY, NOT LIVE)

> **STATUS 2026-06-14 — RESEARCH ARTIFACT ONLY. Nothing here is wired to anything.**
> This note pre-registers the design for testing whether the one Kalshi edge with primary-research
> support — the **maker-side structural edge** (makers earn, takers lose, because retail overpays
> for cheap longshots) — is *capturable by us* net of adverse selection and fees. It is **out of
> the current operating scope**: the subtree is read-only public-data research until **Gate 3**
> (`CLAUDE.md` — no capital, no orders, no separate rails until then). Sourcing of facts:
> `research/kalshi-bot-research.md` (the 2026-06-14 multi-agent web sweep, adversarially verified).
> **Do not request a Market Maker Agreement, place a demo *or* live order, or fund anything off
> this note.** It is a plan to be approved (or killed) before any infra or capital work.

Origin: the 2026-06-14 research sweep concluded that, of every "make money on Kalshi" angle,
**only passive market-making has primary-research support** (Becker 72.1M trades; Bartlett
41.6M trades; GWU/UCD 300k+ contracts). The favorite-buying probe was separately measured to be
no-edge and archived ([`kalshi-near-certain-favorite-FINDINGS.md`](kalshi-near-certain-favorite-FINDINGS.md):
favorites ~3.3¢ overpriced, −3.78%/bet, t=−5.66, robust to drop-top-N). This note is the
disciplined follow-through on the report's §8 recommendation — and is deliberately skeptical,
because the same report flags the edge as *thin, decaying, and infrastructure-gated.*

---

## 1. What we are NOT doing — and why

- **Not directional prediction.** We are not forecasting outcomes. Kalshi's macro markets already
  match professional forecasters' MAE ([NBER w34702]); political markets are the *most*
  miscalibrated but carry the *widest* spreads and maker-taker gaps — highest execution cost
  exactly where calibration is worst. Miscalibration ≠ profit after costs.
- **Not latency arbitrage.** Same standing lesson as the equity book and the Polymarket note:
  **our edge is not speed.** Cross-venue / stale-quote racing is a colocated-HFT game; on a
  Python loop we'd be the slow money being arbitraged.
- **Not naive favorite-buying.** Measured no-edge — favorites are ~3.3¢ *over*priced
  (−3.78%/bet, t=−5.66, **broad and systematic, robust to drop-top-N** — not a skew artifact);
  see [`kalshi-near-certain-favorite-FINDINGS.md`](kalshi-near-certain-favorite-FINDINGS.md).
- **Not the paid Designated Liquidity Provider (DLP) program — yet.** It requires a *signed Market
  Maker Agreement*, is auction-allocated, and bars you from the general Fee Rebate Program. It is a
  post-Gate-3 business decision, not a v1 research target. We test whether the *free* maker edge
  exists before chasing a contractual one.
- **Not cross-platform arb as v1.** Real but small, fleeting, capital-fragmented, and dominated by
  *resolution-criteria mismatch* risk (the same event defined differently on two venues can turn a
  "locked" spread into a total loss). Parked as a later, separate note.

The target is the **passive maker edge**: post resting limit orders (`post_only`) and act as
counterparty to one-sided retail flow, capturing spread + the lower maker fee — **not** predicting,
**not** racing.

---

## 2. The actual hypothesis — capture the maker discount, survive adverse selection

The structural fact (re-verified, High-confidence *as a historical statistic*): on resolved Kalshi
markets makers earn **+1.12%** and takers **−1.12%** excess return, because retail takers overpay
for cheap YES longshots ([Becker]). The gap is largest by volume in **Sports (2.23pp across 43.6M
trades)**. **But this is a wealth-transfer statistic, not a turnkey strategy** — the load-bearing
adversary is **adverse selection**: a resting 48¢ bid is cheaper than crossing *only if filled*,
and you fill disproportionately when fair value just dropped below 48¢ ([whirligigbear]; Bartlett,
[Stanford Law], finds informed traders pick off slow quotes in thin single-name markets). So the
research question is **not** "does the maker edge exist" (it did, historically) but **"does it
survive adverse selection + fees + decay in the series WE can actually quote, on infra WE actually
have?"**

### Pre-registered hypotheses (frozen before any backtest/replay is run)

- **H1 — Net maker edge after adverse selection.** In our target series, a passive
  spread-capturing maker (rest `post_only`, hold to settlement) earns **positive realized return
  net of fees AND net of adverse-selection fill bias.**
  - *Falsifier:* realized maker return ≤ 0 once fills are modeled **adverse-selection-aware**
    (fill only when the quote is at/through subsequent fair value), OR the edge is entirely the
    spread we'd never actually rest inside given queue position. Mid-or-naive-fill backtests are
    disqualified (they assume away the entire risk).
- **H2 — Longshot-fade is the cleanest expression.** Systematically *making the YES side / taking
  NO* against cheap (≤~0.15) YES longshots — the documented "optimism tax" — earns positive
  cost-adjusted return.
  - *Falsifier:* return ≤ 0 after fees on **ask/fill-aware** entries, OR the effect is gone in
    2025-2026 data (decay), OR it survives only by excluding the upset resolutions (drop-top-N
    fails).
- **H3 — Edge concentrates where flow is one-sided, not where spreads are widest.** Per-series
  realized maker return is explained by *retail flow imbalance*, not by raw spread width (wide
  spreads in thin political markets are wide *because* of adverse selection, not free money).
  - *Falsifier:* no relationship between flow imbalance and net maker return; or the only positive
    series are the thinnest/widest ones (i.e. we're being paid for toxicity we can't survive).

All three must clear **after the live per-series fee (queried, not hardcoded — see §3) and a
depth/queue-aware fill model**, and survive the **drop-top-N-winners stress test**. The README's
discipline applies verbatim: *an edge that only survives on mid-price or by excluding upsets is an
artifact.*

---

## 3. Data & method (mirrors the equity book's backtest discipline)

- **The data gap is the whole problem.** Kalshi's public API gives free market data but **no
  official full L2 order-book history** — only 1/60/1440-min candlesticks and a **bids-only**
  orderbook snapshot (NO bid mirrors YES ask). Backtesting a *maker* strategy needs the resting
  book and the fill sequence, which candlesticks cannot reconstruct. So:
  - **Self-collect L2 via WebSocket** (snapshot + `orderbook_delta`, rebuild the book locally) for
    the target series, forward, over a meaningful window — this is a build item, not a download.
  - **Cross-check coverage** against an open dataset (Becker's MIT-licensed 36GB parquet; Lychee's
    ~36GB trades/price set) for sanity, knowing both admit order-book history is incomplete.
- **Fees: query, never hardcode.** Pull `/series/fee_changes` and `/margin/fee_tiers` live. The
  general taker coef is `0.07·p·(1−p)` (peak ~1.75¢ at 0.50), maker is **¼ of that** (~0.0175 coef,
  began Apr 2025), and index series (S&P/Nasdaq) are **half-price (0.035)**. The per-category
  "fee table" circulating online (Crypto ~1.75% / Sports ~1.5% / Politics ~1.4%) is single-sourced
  to one marketing blog, internally inconsistent (1.75% is just the standard 0.07 peak, and the only
  well-documented deviation goes *down* to 0.035 for indices, not up), and unverifiable against the
  official schedule — do not encode it. (Separately: the named maker-fee *subset* some research
  cites — 15/5-min crypto, NCAAB, Serie A — is **Polymarket's** schedule with the maker/taker side
  inverted, not Kalshi's; ignore it too.)
- **Fill model (the part that decides everything):** model fills **adverse-selection-aware** —
  a resting order fills when subsequent fair value reaches/crosses it, i.e. you get the bad fills
  preferentially. Include queue position. This is the analogue of the equity book's depth-aware
  fill rule, and the single most important modeling choice here.
- **Backtest/replay discipline (non-negotiable, same as the equity book):**
  - Pre-register the hypotheses above; freeze the decision rule before looking at returns.
  - Report t-stats **and** the **drop-top-N-winners** stress test (resolutions cluster ⇒ naive t is
    optimistic; bets are not independent).
  - Forward / out-of-sample split on **self-collected** data; no parameter mining to a target.
  - Include voided/disputed/mis-resolved markets (settlement carve-outs are a real loss mode).

---

## 4. Caveats / known traps (read before getting excited)

- **Adverse selection is the strategy's whole risk, and it's invisible to naive backtests.** Any
  result computed on candle mid/ask without modeling *which* orders fill is meaningless-to-flattering.
  This is the #1 way this plan produces a phantom edge.
- **The edge is decaying.** The maker-taker transfer only appeared after Oct 2024 (pro MMs entered)
  and the favorite-longshot ψ is already shrinking in 2025. We may be backtesting an effect that's
  smaller live than in-sample — bias toward recent data and forward replay.
- **Capacity & competition.** The largest-volume maker edge (Sports) faces the most sophisticated
  competition (Jump, Susquehanna provide liquidity). Thin series have edge but the least survivable
  toxicity. There may be no comfortable middle.
- **Demo ≠ production liquidity.** The demo env (`external-api.demo.kalshi.co`) has simulated/thin
  books; a maker fill rate there tells us little about real queue dynamics. Demo proves *plumbing*,
  not *edge*.
- **Operational fragility.** Kalshi has had lockouts/stuck-order incidents during surges; a maker
  with resting inventory must reconcile positions on reconnect and assume the API can drop state.
- **Settlement carve-outs / resolution risk.** The Feb-2026 Khamenei "death-clause" resolved against
  the obvious outcome. Parse resolution criteria per-market; exclude ambiguous ones.
- **New venue ≠ config change.** This is a *separate book* — its own secrets, scheduler, kill switch,
  and (per CLAUDE.md) its own repo at Gate 3 — never a flag on the RH equities engine.
- **Legal/geo + tax are preconditions, not details.** State legality is shifting quarter-to-quarter
  (geofence by residence); event-contract tax treatment is unsettled and the OBBBA 90% gambling-loss
  cap can manufacture phantom income for a high-churn book. Resolve classification with a CPA before
  any churn at scale.

---

## 5. Validation gates (must pass IN ORDER before any capital)

1. **Gate 0 — Legality/rails/access.** Confirm lawful access for the owner's state, a funding path
   isolated from the RH account, and the **API tier** the strategy needs (Basic sustains only
   ~10 writes/s; a quoting bot likely needs Advanced+). Fail → stop here.
2. **Gate 1 — Edge exists in self-collected history.** ≥1 of H1/H2/H3 clears **t>2 after live
   per-series fees AND an adverse-selection-aware, queue-aware fill model**, and survives
   drop-top-N. Mid/naive-fill results do not count. Fail → archive as "no edge," done. *(Expect this
   to be the hard gate — the report rates retail replication of the maker edge only Medium.)*
3. **Gate 2 — Edge persists forward.** Paper/replay on **out-of-sample, self-collected** data and a
   **demo** dry-run of the quoting/cancel/reconnect loop over ≥N resolved markets, in the current
   (post-pro-MM-entry) regime. Fail → archive.
4. **Gate 3 — Owner sign-off** on a separate, small, hard-capped book with its own per-market and
   global exposure caps, daily-loss breaker, kill switch, inventory limits, a price floor
   (never quote contracts < ~$0.15), and self-throttling (exponential backoff on 429 — Kalshi sends
   no `Retry-After`). Split to its own repo per CLAUDE.md. Only then, real capital. *(Only after a
   real maker track record does the paid DLP program become worth evaluating.)*

Default outcome if any gate fails: **this stays a research artifact.** That is an acceptable,
expected result — the report's honest read is that durable retail edge here is thin.

---

## 6. Appendix — minimal plumbing the test needs (build order, demo-only until Gate 3)

From `research/kalshi-bot-research.md` §4/§6 — none of this places a live order:

1. **RSA-PSS signer** (`timestamp_ms + METHOD + path-without-query`, SHA-256, salt = digest length).
   Validate against **demo**. Reuse `kalshi-starter-code-python` / `pykalshi` rather than hand-rolling
   reconnection/backoff.
2. **WebSocket book reconstruction** (snapshot + `orderbook_delta`, bids-only reciprocity, handle
   error code 25 = subscription buffer overflow). This *is* the data collector for §3.
3. **Live fee model** off `/series/fee_changes` + `/margin/fee_tiers`.
4. **Scaffold:** `kapelame/kalshi-crypto-bot` (collector → backtester → paper → live, strategy stub)
   as the structure to fork; `ImMike/polymarket-arbitrage`'s risk-control set
   (`max_position_per_market`, `max_global_exposure`, `max_daily_loss`, kill switch, dry-run) as the
   guardrail template.

---

**One-line bottom line:** The only Kalshi edge with primary-research support is the *maker* side of
the optimism tax — but it's a wealth-transfer statistic, not a strategy, and **adverse selection
(not fees) is the whole risk.** Test it on self-collected L2 with an adverse-selection-aware fill
model, fade-the-longshot as the cleanest expression, every hypothesis gated on drop-top-N and live
fees; expect Gate 1 to be hard, and keep it a research artifact until it isn't.
