---
title: Kalshi / Prediction-Market Trading Bot — Research Report
date: 2026-06-14
scope: prediction-markets/ subtree (research-only; no capital/orders until Gate 3)
method: multi-agent web research (6 dimensions × search→deep-read→adversarial-verify→synthesize), 33 agents
status: reference
---

> **How this maps to the `prediction-markets/` subtree.** This was commissioned for the
> Kalshi research subtree, not the live Robinhood book — same research-only rules apply
> (read-only public data, no orders/wallet until Gate 3). Three direct ties to work already
> in this repo:
>
> 1. **Your ≥0.90 "near-certain favorite" probe is corroborated as a likely no-edge result.**
>    §3.3 below ("optimism tax" / favorite-longshot bias) independently confirms the early
>    unfavorable read in `prediction-markets/README.md`: near-resolution extremes are Kalshi's
>    *best-calibrated* region (little edge), the favorite-longshot ψ-coefficient is *decaying*
>    (smaller & less significant in 2025), and the negative skew is real. Finish the Gate-1
>    backtest in `scripts/kalshi_pull.py`, but the predicted "clears 'data exists,' fails
>    'fee-survivable edge'" outcome looks right — archive it as a documented no-edge result.
> 2. **The recommended v1 edge pivots to passive market-making** (capturing the maker discount
>    on one-sided retail flow), the only edge here with primary-research support — but its real
>    adversary is *adverse selection*, not fees. That's a heavier lift than the favorite idea
>    and would need a new pre-registered hypothesis under your existing backtest discipline
>    (ask-aware fills, t-stats, drop-top-N-winners).
> 3. **The fee math you already encoded (`≈ 0.07·p·(1−p)`) checks out** (§2) — with the nuance
>    that it's a per-fill round-*up* to the cent, indices are half-price (0.035), and the
>    per-category fee table circulating online is unverified. Pull live `/series/fee_changes`
>    rather than hardcoding.
>
> The **§4 "Making money with the API"** section is kept as forward-looking reference
> (auth/endpoints/order types/MM programs) — Gate-3 material, consistent with the subtree's
> no-live-execution rule, not something to wire up now.

---

# Kalshi / Prediction-Market Trading Bot: Research Report

*Prepared for a developer building an automated Kalshi trading bot. Every claim below is drawn from the structured research provided, with adversarial-verification verdicts applied to caveat, correct, or drop unsupported items. Confidence labels (High/Med/Low) are attached to strategy and profit claims. Sources are cited inline.*

---

## 1. TL;DR — the honest bottom line

**Is there real edge for a retail bot? Some — but it is thin, structural, and infrastructure-gated, not "free money."** The single best-documented, durable edge is *structural, not predictive*: on resolved Kalshi markets, **makers earn a positive excess return and takers lose** because retail takers overpay for cheap YES longshots — the "optimism tax." Jonathan Becker's 72.1M-trade study puts the split at takers **−1.12%** vs makers **+1.12%** excess return ([The Microstructure of Wealth Transfer in Prediction Markets](https://www.jbecker.dev/research/prediction-market-microstructure)). Academic work confirms a favorite-longshot bias on 300k+ contracts — sub-10¢ contracts lose 60%+, and **the average Kalshi contract returns roughly −20%** ([Makers and Takers, GWU/UCD](https://www2.gwu.edu/~forcpgm/2026-001.pdf)).

**The honest caveats that gut most of the hype:**

- **The maker edge is a wealth-transfer statistic, not a turnkey strategy.** Capturing it requires *real quoting infrastructure* because resting (maker) orders are adversely selected — you get filled disproportionately when fair value just moved against you ([whirligigbear, Maker/Taker Math](https://whirligigbear.substack.com/p/makertaker-math-on-kalshi)). Verifiers stressed this: "naively copying it (always making/selling longshots) ignores adverse selection, fees, and capital/queue risk."
- **The bias is decaying.** The favorite-longshot ψ-coefficient "is smaller and less statistically significant" in 2025 data, and the maker-taker transfer only appeared *after* Oct 2024 when professional market makers entered ([Becker](https://www.jbecker.dev/research/prediction-market-microstructure); [GWU/UCD](https://www2.gwu.edu/~forcpgm/2026-001.pdf)).
- **Fees bite hardest exactly where edges are thinnest.** The taker fee peaks at ~1.75¢/contract at the 50¢ midpoint, so a taker round-trip needs ~3.5¢ of edge before spread ([Polytrage](https://blog.polytrage.com/kalshis-fee-structure-explained/)).
- **Naive directional bots lose.** A documented weather-bot postmortem went **0 wins / 32 losses** to fees, fat tails, and slow polling ([Northlake Labs](https://www.northlakelabs.com/max/blog/kalshi-weather-postmortem-and-pivot/)).
- **"Risk-free arbitrage" is mostly marketing.** Cross-venue arb is real but small, fleeting, capital-fragmented, and dominated by *resolution-criteria mismatch* risk that can turn a "locked" spread into a total loss ([CoinDesk](https://www.coindesk.com/markets/2026/02/21/how-ai-is-helping-retail-traders-exploit-prediction-market-glitches-to-make-easy-money)).

**Where edge actually lives, ranked by realism:** (1) passive market-making / liquidity provision where retail flow is one-sided — best in sports by volume, but you must survive adverse selection and drawdowns; (2) Kalshi's **paid** Designated Liquidity Provider program (gated behind a signed Market Maker Agreement); (3) base-rate-divergence trades in retail-dominated macro/political markets; (4) low-capacity cross-platform arb *if* you encode resolution-criteria matching. **Where it does not:** out-forecasting the Fed (Kalshi macro markets are as accurate as professional forecasters — [NBER w34702](https://www.nber.org/system/files/working_papers/w34702/w34702.pdf)), naive weather/longshot betting, and anything sold as "guaranteed."

**Main risks:** parabolic fees at mid-book, adverse selection on resting orders, thin liquidity/legging risk, settlement carve-outs that resolve against the obvious outcome (the Feb 2026 Khamenei death-clause), a shifting state-by-state legal map requiring geofencing, and unsettled tax treatment (the OBBBA 90% gambling-loss cap can manufacture phantom income for a high-churn bot).

---

## 2. How Kalshi works & the fee math

### Contract structure
Kalshi trades **binary Yes/No event contracts** on a **CFTC-regulated central-limit-order-book exchange** (KalshiEX LLC, a Designated Contract Market). Contracts are priced 1–99¢ and settle to **$1 (correct) or $0 (wrong)** per a published Expiration/Payout Criterion once Kalshi confirms the official result ([Britannica](https://www.britannica.com/money/Kalshi-Inc)). Resolution source varies by category — BTC markets reference CF Benchmarks, econ markets the official data release, sports the official game result — **so a bot must read the per-series settlement source rather than assume** (High confidence).

A quirk for book reconstruction: the orderbook endpoint **returns only bids**, because of the reciprocal YES/NO relationship (a NO bid is the mirror of a YES ask) ([Kalshi Market Data docs](https://docs.kalshi.com/getting_started/quick_start_market_data)).

### The fee formula (verified)
**Taker fee = `round_up(0.07 × C × P × (1−P))`** per fill, rounded UP to the next cent, where C = contracts and P = price in dollars. Because P×(1−P) maxes at 0.25 at P=0.50:

- **Peak taker fee ≈ $0.0175/contract at 50¢** (1.75% of the $1 notional; 3.5% measured against the 50¢ price)
- ~$0.0063/contract at 10¢/90¢
- Falls toward zero at the tails

This is confirmed across the official help center, the [Polytrage breakdown](https://blog.polytrage.com/kalshis-fee-structure-explained/), and [whirligigbear's worked derivation](https://whirligigbear.substack.com/p/makertaker-math-on-kalshi) (49¢: 0.07×0.49×0.51 = 1.75¢, net price 50.75¢). Verifiers independently confirmed the taker math (High confidence).

**Worked example (100 contracts):**

| Price | P×(1−P) | Taker fee (100 contracts) |
|------:|--------:|--------------------------:|
| 50¢ | 0.2500 | $1.75 |
| 30¢/70¢ | 0.2100 | $1.47 |
| 10¢/90¢ | 0.0900 | $0.63 |

The **round-up-to-the-cent is per fill on the aggregate** (round_up of the whole 0.07×C×P×(1−P) product), not per single contract — so larger fills amortize the 1¢ floor, while a 1-contract fill on a 5¢ tail contract pays the full 1¢ (a 20% tax on price).

### Maker fees — corrected
- **Maker fee = `round_up(0.0175 × C × P × (1−P))`** — exactly **one quarter of the taker coefficient** — peaking at ~$0.0044/contract at 50¢ ([marketmath.io reading of the official schedule](https://marketmath.io/platforms/kalshi)). Maker fees only began being charged after **April 2025**.
- **REFUTED claim — flag it:** Earlier research framed maker fees as applying to a named subset of "15/5-minute crypto, NCAAB after Feb 18 2026, and Serie A after Feb 18 2026." **The verifier refuted this: that named subset is *Polymarket's* fee schedule, not Kalshi's, and on Polymarket the *maker gets a rebate while the taker pays* — the side is inverted.** Do not encode those series as Kalshi maker-fee markets. What *is* supported: on Kalshi, most markets charge takers only, some special-event series carry a small maker fee, and the precise Kalshi maker-fee-bearing series could not be verified against the (rate-limited) official PDF. ([Kalshi Fees help center](https://help.kalshi.com/en/articles/13823805-fees); verifier sources include [Polymarket changelog](https://docs.polymarket.com/changelog) and [Bitget](https://www.bitget.com/news/detail/12560605198122).)
- **Per-category fee variation — PARTLY verified, framing was wrong.** The directional thesis is sound: *fees vary by series; don't hardcode one coefficient; pull live per-series params.* This is officially supported (the help center says some markets have different fees "due to special events"), and there is a documented **0.035 coefficient (half-price) for S&P 500 / Nasdaq-100 index markets** ([Kalshi: "We're Halving the Fees"](https://news.kalshi.com/p/were-halving-the-fees)). **But the specific per-category peak-fee table (Crypto ~1.75% / Sports-Econ ~1.5% / Politics-Weather ~1.4%) is single-sourced to one marketing blog, unverifiable against the official schedule, and internally inconsistent** — 1.75% *is* the standard 0.07 peak, and the only well-documented deviation goes *down* (0.035 for indices), not up ([PredictionHunt](https://www.predictionhunt.com/blog/kalshi-fees-complete-guide-2026); contradicted by [Polytrage](https://blog.polytrage.com/kalshis-fee-structure-explained/) and [marketmath.io](https://marketmath.io/platforms/kalshi), which assert "one clean curve"). **Action: pull the live `/series/fee_changes` and `/margin/fee_tiers` endpoints; treat the category percentages as unverified.**

### No settlement fees
Once a contract resolves to $1 or $0, **no additional fee is charged** ([Kalshi Fees](https://help.kalshi.com/en/articles/13823805-fees)) — mechanically consistent, since 0.07×P×(1−P) = $0 at P=1.00 or 0.00. **Holding to settlement avoids the exit fee**, roughly halving a position's total fee burden vs an early round-trip exit (High confidence).

### What a trade must beat to be +EV
- **Mid-book (~50¢) taker round-trip:** ~$0.0175 entry + ~$0.0175 exit ≈ **3.5¢/contract = 3.5% of max payout**, plus the bid-ask spread. Break-even win-probability shift ≈ **1.75% before spread** ([Polytrage](https://blog.polytrage.com/kalshis-fee-structure-explained/), High confidence).
- **Passive maker entry held to settlement:** near-zero fees — needs only to beat the spread plus model error. *This is the single biggest fee lever*, traded off against fill uncertainty and adverse selection.
- **Tail contracts (1–5¢ / 95–99¢):** cheapest in absolute fee terms, but the 1¢ round-up floor makes them expensive *as a % of price* — the documented rule of thumb is **"never trade contracts below ~$0.15"** ([Northlake Labs](https://www.northlakelabs.com/max/blog/kalshi-weather-postmortem-and-pivot/)).

### Liquidity & spreads (the real cost in thin markets — PARTLY verified)
Spreads vary widely: flagship/sports markets run ~1–4¢ (Super Bowl-type ~1¢), low-profile markets ~8–15¢ with shallow depth. **In illiquid markets, spread and slippage dominate fees** — fee is a near-fixed ≤1.75% cost while spread can be 5–15%+ of payout ([DeFiRate](https://defirate.com/prediction-markets/how-order-books-work/)). Market-making is provided by firms like **Jump Trading** and Susquehanna ([Bloomberg](https://www.bloomberg.com/news/articles/2026-02-09/jump-trading-poised-to-gain-stakes-in-kalshi-and-polymarket)). **Verifier caveats:** the "5¢ mispricing can persist for days" claim is asserted, not sourced — most arbitrage windows on watched markets close in seconds-to-minutes; persistence applies only to genuinely stale/illiquid markets. The specific spread bands are directionally right but hand-picked, not from a primary measurement (Med confidence).

### Deposits/withdrawals — corrected
ACH and wire are **free on Kalshi's side**; **debit-card *deposits* carry ~2%**, but — **correcting the earlier research** — **debit-card *withdrawals* are FREE** per Kalshi's own help center (funds arrive ~30 min). The verifier flagged the prior "2% on both" as a fabricated agreement leaning on a weak aggregator over Kalshi's primary docs ([Kalshi: Debit Card Withdrawals](https://help.kalshi.com/en/articles/13823802-debit-card-withdrawals); [Card Deposits](https://help.kalshi.com/en/articles/13823795-card-deposits)). **Fund a bot via free ACH/wire and pre-position capital** — ACH can settle over several business days and new deposits may carry security holds ($10 deposit min / $1,000 wire min). Crypto has no Kalshi fee but passes through network fees, disclosed pre-transaction.

---

## 3. Strategies with real edge (ranked, realism-first)

> **Reading the labels:** "Edge" = documented/structural; "Confidence" = how well-supported the *profitability for a retail bot* is after fees, adverse selection, and competition. A high-confidence *finding* (e.g. the maker-taker statistic exists) can still be a medium- or low-confidence *strategy* because replication is hard.

### 3.1 Passive market-making / liquidity provision (the strongest documented edge)
**How it works:** Post resting limit (maker) orders with `post_only`, capturing the spread plus the lower maker fee, acting as counterparty to one-sided retail flow rather than predicting outcomes.
**Edge source:** The maker-taker wealth transfer (makers +1.12%, takers −1.12% excess return; per-category gap largest in Sports at 2.23pp across 43.6M trades — the highest-*volume* maker-edge category) ([Becker](https://www.jbecker.dev/research/prediction-market-microstructure)). Sports "underwriting" reportedly produced ~$29M aggregate LP profit across one NFL season ([Frenzy Capital](https://medium.com/@FrenzyCapital/trading-strategies-for-prediction-markets-4025a050e2e2)).
**The catch (load-bearing):** Adverse selection. A 48¢ limit bid is cheaper than crossing *if filled* — but you fill mostly when fair value just dropped below 48¢ ([whirligigbear](https://whirligigbear.substack.com/p/makertaker-math-on-kalshi)). Bartlett's 41.6M-trade study confirms informed traders pick off slow quotes in thin single-name markets, where makers earn ~2× per contract but face flow toxicity ([Stanford Law](https://law.stanford.edu/publications/adverse-selection-in-prediction-markets-evidence-from-kalshi/)). You *absorb* directional inventory, you don't eliminate it — expect weekly drawdowns and blow-up risk on correlated adverse events (a chalk-heavy slate all hitting).
**Capital/skill:** High. Real quoting infrastructure, WebSocket book reconstruction, inventory/risk management, and a capital base that survives portfolio drawdowns. Sports needs deep books but faces the most sophisticated competition.
**Confidence: Medium** (aggregate edge documented at High confidence; *individual retail replication* is Medium-to-questionable — verifiers repeatedly flagged the dollar magnitudes as secondary/directional).

### 3.2 Kalshi's Designated Liquidity Provider program — a paid edge (gated)
**How it works:** Execute a **Market Maker Agreement** with Kalshi, become a Designated Liquidity Provider on an Incentivized Series, and earn an Incentive Period Reward allocated via **auction** (you bid the minimum reward you'll accept) ([Kalshi LP Program](https://help.kalshi.com/en/articles/15410219-liquidity-provider-program)).
**Edge source:** Direct payment for quoting. Incentivized series include NYC hourly weather, commodities (WTI, Brent, corn, gold, silver, copper), 15-minute alt-crypto, and event markets (Truth Social / Trump-mention); most series have only **1–2 designated providers**, implying low competition for those who qualify (termination date 6/31/26; auctions rotate). **Verified by the help center.**
**Caveats (flagged):** **No dollar figures are published** — the "~$35K/day (~$12.7M/yr)" figure circulating online traces to an *uncited blog* and should be treated as fabricated/directional. The gating step (a signed MMA) is a material barrier, and requires the Premier or Market Maker API tier. Note this program is **mutually exclusive** with the general Fee Rebate Program (MMA holders are barred from the rebate program) ([CFTC filing](https://www.cftc.gov/filings/orgrules/rules01132513688.pdf)).
**Capital/skill:** High + a business relationship with Kalshi.
**Confidence: Medium** (program existence High; economics undisclosed).

### 3.3 Longshot / favorite-bias (the "optimism tax")
**How it works:** Systematically take the **NO** side (or make the YES side) of cheap longshots where retail piles into YES.
**Edge source:** At 1¢ prices, YES won only 0.43% of the time vs 1% implied (−41% EV for 1¢ YES vs +23% for 1¢ NO — a 64pp gap); **NO outperformed YES at 69 of 99 price levels**, concentrated at the extremes ([Becker](https://www.jbecker.dev/research/prediction-market-microstructure)). Independently confirmed: sub-10¢ contracts lose 60%+ of money; the average contract returns ~−20% ([GWU/UCD](https://www2.gwu.edu/~forcpgm/2026-001.pdf)).
**The catch:** (1) The bias is **decaying** — the ψ-coefficient is "smaller and less statistically significant" in 2025. (2) The 1¢ fee floor is brutal on tail contracts — *but* taking NO at 1¢ means buying at 99¢ where the absolute fee is small; size matters. (3) Densest in entertainment/media/world-events, thinnest in finance (0.17pp gap). (4) Verifier on the related calibration claim: "fade the bias" is not enough — slope>1 (compression toward 50%) means you need a real edge *over* the crowd's probability, and large trades in political markets push price *against* you ([calibration study, partly verified](https://arxiv.org/abs/2602.19520)).
**Capital/skill:** Medium. Mostly a scanning + sizing problem; the hard part is avoiding the markets where the bias has already been arbed out.
**Confidence: Medium** (the bias is High-confidence historically; the forward-looking *tradeable* edge is Medium-and-shrinking).

> **Subtree note:** this is the section that bears on your `kalshi_pull.py` ≥0.90-favorite probe. The "buy the near-certain favorite, hold to settlement" idea sits on the *favorite* tail of exactly this bias — and the evidence says the extreme tails are where Kalshi is *best* calibrated (least edge) and where one upset wipes many small wins. Consistent with your README's early unfavorable read.

### 3.4 News-driven / base-rate divergence in macro & political markets
**How it works:** Trade retail recency/narrative bias back toward the historical base rate in retail-dominated macro (FOMC, CPI, jobs) and political contracts; secondarily, exploit Kalshi-vs-fed-funds-futures distributional divergence.
**Edge source:** Long-horizon and political markets are the **most miscalibrated** — a calibration study explains 87.3% of variance with a universal horizon effect compressing prices toward 50% and political markets underconfident at nearly all horizons ([arXiv 2602.19520](https://arxiv.org/abs/2602.19520), re-verified against the HTML primary). Dovetails with Becker's 0.17pp Finance gap and the low-price-tail bias.
**The hard truth (verifier correction):** **You will NOT out-forecast the Fed.** Kalshi macro forecasts match professional forecasters' MAE ([NBER w34702](https://www.nber.org/system/files/working_papers/w34702/w34702.pdf)). And **"most exploitably mispriced" overreaches** — political markets carry the *widest* maker-taker gaps (>7pp in some categories) and widest spreads, i.e. the highest execution cost exactly where calibration is worst. Miscalibration ≠ profit after fees/slippage.
**Capital/skill:** High (you need a genuine model edge, not mechanical bias-trading).
**Confidence: Low-to-Medium** (miscalibration is well-evidenced; converting it to net-of-cost profit is unproven for a retail bot).

### 3.5 Cross-platform arbitrage (Kalshi vs Polymarket)
**How it works:** Detect the same event priced differently across venues, or YES+NO summing to <$1.00, and lock the spread.
**Edge source / reality:** Real but **small, fleeting, capital-constrained, and dominated by resolution-criteria risk, not execution** (verified supported). The dominant risk is two venues defining "the same event" differently — Kalshi settles crypto on CF Benchmarks/CME-CF, Polymarket on a CoinGecko-style spot via UMA oracle; a Cardi B Super Bowl market is a cited divergence — so a "risk-free" spread can become a **total loss** ([CoinDesk](https://www.coindesk.com/markets/2026/02/21/how-ai-is-helping-retail-traders-exploit-prediction-market-glitches-to-make-easy-money)).
**Verifier corrections to the magnitudes:** The widely-cited "8,894 trades / ~$150K / ~$16.80 per trade / milliseconds / $5–15K depth" figures describe a **single-venue Polymarket YES+NO<$1 glitch bot, NOT cross-platform Kalshi-vs-Polymarket arb** — and the ~$150K was *profit*, not volume (turnover ≈ $8.9M). Don't characterize cross-venue arb with single-venue stats. On Polymarket itself, a peer-reviewed paper (IMDEA/Oxford, AFT 2025) confirms **~$40M extracted Apr 2024–Apr 2025** via market-rebalancing and NegRisk combinatorial arb ([arXiv:2508.03474](https://arxiv.org/abs/2508.03474)) — **but this is a Polygon/on-chain finding that does NOT transfer to Kalshi** (centralized, no on-chain tooling/NegRisk bundling). The "14 of 20 top wallets are bots" stat is a leaderboard tweet, not the paper.
**Capital/skill:** High + an on-chain Polygon/USDC leg, pre-funded both sides. Legging risk (filled on one venue, stuck on the other) turns arb into directional exposure.
**Confidence: Low-to-Medium** (mechanism real; net retail profitability constrained by depth, speed, capital fragmentation, and resolution risk).

### 3.6 Documented loser — naive recurring-weather betting
A first-person postmortem went **0–32** ([Northlake Labs](https://www.northlakelabs.com/max/blog/kalshi-weather-postmortem-and-pivot/)): the 5¢-contract fee death zone (1¢ fee = 20% tax, needing ≥83.3% hit-rate to break even), Gaussian blindness (NWS fat tails make a "2-sigma" event show up 10–12% of the time, so "90% certainty" was really ~75–80%), and the exit-liquidity trap (15–60-min polling vs weather-arb bots that move within seconds). **Confidence that this loses: High.** Include as a guardrail, not a strategy.

---

## 4. Making money with the Kalshi API

### Authentication — RSA-PSS request signing (not OAuth)
Every authenticated request carries three headers ([Kalshi API Keys](https://docs.kalshi.com/getting_started/api_keys), verified supported):

- `KALSHI-ACCESS-KEY` = the Key ID
- `KALSHI-ACCESS-TIMESTAMP` = Unix **milliseconds**
- `KALSHI-ACCESS-SIGNATURE` = base64( RSA-PSS sign of `timestamp_ms + HTTP_METHOD + path` )

**Signing details (get these exactly right or every call 401s):**
- Sign the **path *without* query params and without host** — e.g. sign `/trade-api/v2/portfolio/orders`, not `…?limit=5`.
- Algorithm: RSA-PSS with SHA-256, MGF1/SHA-256, **salt length = digest length (32 bytes)**. Python: `padding.PSS(mgf=MGF1(SHA256()), salt_length=PSS.DIGEST_LENGTH)`. JS: `RSA_PKCS1_PSS_PADDING` + `RSA_PSS_SALTLEN_DIGEST`.
- Generate the key pair in account settings; the **RSA private key (PEM) is shown once and never retrievable** — save it immediately. Clock sync matters (the timestamp is part of the signed payload).

### Environments / base URLs
- **Production REST:** `https://external-api.kalshi.com/trade-api/v2` (recommended; `api.elections.kalshi.com/trade-api/v2` also supported — and despite the "elections" subdomain, serves *all* categories)
- **Production WebSocket:** `wss://external-api-ws.kalshi.com/trade-api/ws/v2`
- **Demo REST:** `https://external-api.demo.kalshi.co/trade-api/v2` (fake money, separate keys, same signing flow)
- **Demo WS:** `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`

([API Environments](https://docs.kalshi.com/getting_started/api_environments)). Demo liquidity may be simulated/thin, so live fills can differ.

### Key endpoints
- **Market data (no auth):** `GET /series/{ticker}`, `GET /markets` (filter by series/status), `GET /events/{ticker}`, `GET /markets/{ticker}/orderbook` (**bids only** — reconstruct two-sided depth from YES/NO reciprocity). Cursor-paginated ([Market Data docs](https://docs.kalshi.com/getting_started/quick_start_market_data)).
- **Candlesticks:** `GET /series/{series_ticker}/markets/{ticker}/candlesticks` (series-nested), with `start_ts`, `end_ts`, `period_interval` ∈ **{1, 60, 1440} minutes only**. OHLC sub-objects for `price`, `yes_bid`, `yes_ask`, plus `volume_fp`/`open_interest_fp` (fixed-point). Settled markets are under `GET /historical/markets/{ticker}/candlesticks`. **No sub-minute official OHLC and no official L2 book history** ([Get Market Candlesticks](https://docs.kalshi.com/api-reference/market/get-market-candlesticks)).
- **Orders:** `POST /portfolio/events/orders` (Create Order V2); track via `GET /portfolio/orders`; positions via `get_positions`.
- **Fees (query, don't hardcode):** `GET /trade-api/v2/series/fee_changes`, `GET /trade-api/v2/margin/fee_tiers`.
- **Account/limits:** `GET /account/limits`, `GET /account/endpoint_costs`.

### Order types (Create Order V2)
Required fields: `ticker`, `side` (BookSide `bid`/`ask` — refers to the **YES leg**: bid = buy YES, ask = sell YES; selling YES = buying NO at 1−price), `count`, `price`, `time_in_force`, `self_trade_prevention_type`. Optional: `client_order_id`, `expiration_time`, `post_only`, `reduce_only`, `subaccount`, `order_group_id` ([Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2)).

- **Time in force:** `fill_or_kill`, `immediate_or_cancel`, `good_till_canceled`.
- **`post_only=true`** prevents immediate matching (cancels rather than taking) — **the lever for capturing the maker fee**.
- **Self-trade prevention:** `taker_at_cross` or `maker`.
- Mapping: rest GTC limits with `post_only` for maker fills; FOK/IOC for taker fills.

### WebSocket feeds
Auth signs `timestamp + 'GET' + '/trade-api/ws/v2'` at handshake. Channels — **private:** `orderbook_delta`, `fill`, `market_positions`, `order_group_updates`, `communications`; **public:** `ticker`, `trade`, `market_lifecycle_v2`, `multivariate_market_lifecycle`. Subscribe: `{"id":N,"cmd":"subscribe","params":{"channels":[...],"market_ticker":"X"}}` (use `market_tickers:[...]` for many). Rebuild a local book from snapshot + `orderbook_delta`. Error code 25 = subscription buffer overflow during bursts — subscribe only to needed markets ([WebSockets docs](https://docs.kalshi.com/getting_started/quick_start_websockets)).

### Rate limits (corrected)
Dual token-bucket (separate **Read** and **Write** budgets), per tier; **default cost 10 tokens/request, cancels 2** ([Rate Limits and Tiers](https://docs.kalshi.com/getting_started/rate_limits), verified supported):

| Tier | Read tok/s | Write tok/s | ~Orders/s (Write) |
|------|-----------:|------------:|------------------:|
| Basic | 200 | 100 | ~10 |
| Advanced | 300 | 300 | ~30 |
| Premier | 1,000 | 1,000 | ~100 |
| Paragon | 2,000 | 2,000 | ~200 |
| Prime | 4,000 | 4,000 | ~400 |

**Correction to earlier research:** Basic sustains **~10 writes/sec, not 100/sec.** Over-limit returns **HTTP 429 `{"error":"too many requests"}` with no `Retry-After` / `X-RateLimit-*` headers and no cooldown penalty** — the bot must **self-throttle** with exponential backoff. Write buckets above Basic hold 2 seconds of budget (≈2× burst after idle). Basic is automatic; Advanced is self-serve via the "Upgrade Account API Usage Level" endpoint; Premier/Paragon/Prime are earned from 30-day volume share (earn/keep thresholds: 0.25%/0.20%, 0.50%/0.40%, 1.00%/0.80%). **Prefer WebSocket deltas over polling.**

### Market-maker rebate / liquidity programs (two distinct tracks — don't conflate)
1. **Fee Rebate / Volume Rewards Program** — tiered rebate on taker *and* maker fees (e.g. 60% on the $750.01–$2,000 fee band, 80% above), weekly cap commonly cited ~$7,000. **Excludes** Kalshi affiliates, Market Maker Agreement holders, and the **Sports** category ([CFTC filing](https://www.cftc.gov/filings/orgrules/rules01132513688.pdf), Med confidence — exact cap varies by iteration).
2. **Designated Liquidity Provider Program** — requires a signed MMA; auction-allocated; 1–2 providers per series (see §3.2). Mutually exclusive with track 1.

### SDKs (official + community)
- **Official:** `kalshi-python` on PyPI self-describes as the official SDK (**v2.1.4, Sept 2025**, maintained by Kalshi); Swagger-Codegen-generated. The docs also name `kalshi_python_sync` / `kalshi_python_async` and `kalshi-typescript`, with the **old `kalshi-python` package deprecated** — *verify which package you're pinning* (the PyPI vs deprecated-package naming is genuinely confusing). Install from PyPI/npm, not by cloning the GitHub org ([SDKs overview](https://docs.kalshi.com/sdks/overview); [PyPI](https://pypi.org/project/kalshi-python/)).
- **Treat the OpenAPI (REST) / AsyncAPI (WebSocket) specs as the source of truth** — `docs.kalshi.com/llms.txt` indexes them; raw `openapi.yaml`/`asyncapi.yaml` (+ perps variants) are downloadable.
- **Community:** `pykalshi` (105★) adds the production glue the official SDK lacks — WebSocket streaming, retries with backoff, rate-limit handling, pandas `.to_dataframe()`, and a local `OrderbookManager` ([pykalshi](https://github.com/arshka/pykalshi)).
- **Official starter:** `kalshi-starter-code-python` (a working signed-request reference) ([github.com/Kalshi](https://github.com/Kalshi)).

### Getting-started path
1. Generate the RSA key pair in account settings; save the private PEM immediately.
2. Implement the `timestamp+METHOD+path` RSA-PSS signer (or use the official SDK / starter repo).
3. Point at **demo** (`external-api.demo.kalshi.co`) and validate order placement / cancellation / fills with fake money.
4. Build WebSocket book reconstruction (snapshot + `orderbook_delta`); self-throttle to your tier.
5. Graduate to production with a production key — start small, since demo liquidity differs.

---

## 5. Accounts & communities to follow / copy-trading reality

### The truth about copy-trading on Kalshi
**You cannot natively copy or mirror trades on Kalshi.** Kalshi has **no copy/mirror feature and no social feed.** Its only social surface is an **opt-OUT-by-default leaderboard** that exposes only a name and performance ranking — **no positions, no trade history** ([Kalshi Leaderboard](https://help.kalshi.com/en/articles/13823809-leaderboard)). "Kalshi copy trading" is a **misnomer**: "Kalshi exposes no trader identity, so there is no one to mirror in the literal sense" ([botforkalshi guide](https://www.botforkalshi.com/blog/kalshi-trading-bots-complete-guide)). This is the opposite of Polymarket, where every position lives on a public Polygon wallet and is trivially copyable. The real native automation surface is the API (§4).

### High-signal accounts (with credibility flags)
- **[@Domahhhh](https://x.com/Domahhhh)** — "Domer," the #1 Polymarket trader, corroborated by CBS 60 Minutes / DL News / OnChainTimes (called the JD Vance VP pick; $3M+ profit). The default highest-signal follow. *Verified supported; activity has cooled from the 2023–24 peak.*
- **[@ssgamblers / Pratik Chougule](https://www.youtube.com/@starspangledgamblers1029)** — Star Spangled Gamblers, the leading political-betting podcast. Best *qualitative* signal for election/legislation markets; **not a quant feed.** *Verified supported.*
- **[whirligigbear / Andrew Courtney](https://whirligigbear.substack.com/)** — ex-Susquehanna/Crypto.com quant; rigorous maker/taker fee + adverse-selection math (the most load-bearing follow for MM bot economics). **Flag:** openly-disclosed commercial conflict — runs Kalshinomics.com (Kalshi affiliate), so a mild pro-Kalshi incentive. *Verified supported.*
- **[@predictionquant](https://substack.com/@predictionquant)** — research on signal quality, microstructure, and cross-platform arb. Most directly useful for a quant builder. *Med confidence.*
- **[Kalshi Discord](https://discord.com/invite/kalshi)** — official community; a place to vet third-party bots before trusting them with keys.

### Crowd-sourced specialist list — handles real, dollar figures NOT
A widely-shared [@aulijk post](https://x.com/aulijk/status/1996612322496823679) names Kalshi specialists: @GaetenD (culture/entertainment, ~$500K), @cobybets1 (sports+politics, ~$640K), @aenews_KT (news, ~$450K), @Foster, @debl00b, @theduckguesses. **The handles are real and worth vetting; every profit figure comes from a single crowd-sourced post, not audited data — treat as social claims only** (Med confidence).

### Third-party copy-trade layers
- **[FrenFlow](https://www.frenflow.com/)** — the most credible third-party social-copy layer (now 4 venues: Polymarket, Kalshi via DFlow-on-Solana, Predict.fun, Hyperliquid). Non-custodial (Privy-backed Safes / embedded wallets); fees are a Polymarket builder fee of 1% taker / 0.5% maker (no other service fees). **Flag: open beta, unaudited, no named team, <1-year track record — paper/small-size first** (Med confidence).
- **Trackers for discovery only:** [predicting.top](https://predicting.top/) lists "top traders," but **Kalshi positions are not on-chain, so its Kalshi numbers are unverifiable** — useful for finding names, not audited P&L (Med confidence).

### LOW-trust — do not connect keys or funds
- **PR-newswire "Kalshi copy-trading bot" products** (e.g. kalshitradingbot.net) launched via King Newswire / Binary News Network press releases, tagged "PRESS RELEASE" with disclaimers, generic contacts, zero independent reviews, and no Kalshi affiliation ([digitaljournal PR](https://www.digitaljournal.com/pr/news/binary-news-network/kalshi-trading-bot-launches-advanced-1945303017.html)). Classic "make money fast" markers (High confidence: low trust).
- **Hacked custodial copy services — PLATFORM-MISMATCH FLAG:** earlier research listed "Polycule" (~$230K stolen Jan 2026) under Kalshi. **The verifier corrected this: Polycule is a *Polymarket* Telegram copy-bot, not Kalshi.** Don't present Polymarket-specific scam patterns as Kalshi facts. The general lesson (custodial copy services are a wallet-drain vector) stands ([KuCoin](https://www.kucoin.com/news/flash/telegram-trading-bot-polycule-on-polymarket-hacked-230k-stolen)).

---

## 6. Existing bots, data sources & backtesting

### Open-source repos (auditable — strongly preferred over closed PR-launched bots)
- **[kapelame/kalshi-crypto-bot](https://github.com/kapelame/kalshi-crypto-bot)** (~5★) — the most complete end-to-end scaffold: collector (Kalshi + Coinbase every 2s → SQLite/CSV), data-quality checker, XGBoost ML (accuracy/Brier/calibration/simulated PnL), walk-forward backtester, paper trader, live trader (RSA auth), terminal dashboard. Ships ~2,500 sample snapshots. **Strategy logic is intentionally an empty stub — bring your own signals.** Best starting scaffold (High confidence it's a good plumbing reference).
- **[ImMike/polymarket-arbitrage](https://github.com/ImMike/polymarket-arbitrage)** (190★) — most-starred cross-venue arb bot. Scans 10,000+ markets; three strategies (cross-platform, YES+NO bundle, market-making); text-similarity market matching (`min_match_similarity: 0.6` — the error-prone part); FastAPI dashboard; risk controls (`max_position_per_market`, `max_global_exposure`, `max_daily_loss`, kill switch, dry-run). README is candid: *"Arbitrage opportunities are rare and fleeting."*
- **[ryanfrigo/kalshi-ai-trading-bot](https://github.com/ryanfrigo/kalshi-ai-trading-bot)** — signed client, data ingestion, SQLite telemetry, Streamlit dashboard, pluggable LLM client, three example strategies. Maintainers warn edges are small and no strategy is guaranteed profitable.
- **[yllvar/Kalshi-Quant-TeleBot](https://github.com/yllvar/Kalshi-Quant-TeleBot)** — Prometheus/Grafana dashboards + Telegram control (the common monitoring pattern).

### Backtesting tooling (data, not the engine, is the bottleneck)
- **[quantgalore/kalshi-trading](https://github.com/quantgalore/kalshi-trading)** — readable single-file backtest; lowest-ceremony way to learn entry/exit and $0/$1 settlement modeling.
- **[evan-kolberg/prediction-market-backtesting](https://github.com/evan-kolberg/prediction-market-backtesting)** — NautilusTrader adapter for unified backtest-and-live; explicit caveat that **meaningful Kalshi backtesting depends on reconstructed L2 book history the official API doesn't provide** (Med confidence — repo-level claims from the curated index).
- **[aarora4/Awesome-Prediction-Market-Tools](https://github.com/aarora4/Awesome-Prediction-Market-Tools)** (485★, 19 categories) — the single best map of the landscape (APIs, aggregators, arb tools, backtesters, alerting).

### Historical / orderbook data
The official API gives free unauthenticated market data but **no convenient deep history and no official full L2 book history** — meaningful backtesting depends on **self-collected WebSocket data or third-party vendors**, all of whom admit order-book history is incomplete:
- **[Lychee](https://lycheedata.com/guides/kalshi-historical-data)** — ~36GB dataset (trades, metadata, price history since launch ~2021); CSV/JSON/XLSX; states plainly "Order book history is typically harder to obtain because snapshots are not always stored."
- **Dome, Probalytics (claims 200–500M orderbook updates/day, ClickHouse SQL), Marketlens (tick-level, Polymarket-focused)** — treat granularity/latency claims as marketing until benchmarked.
- **Becker's study data** is fully open-sourced (MIT, 36GB parquet on R2) — a primary dataset to mine ([repo](https://github.com/Jon-Becker/prediction-market-analysis)).

### Data quirks a backtester must handle
Candlesticks only at 1/60/1440-min granularity; orderbook returns bids-only; `_fp` fixed-point fields; contracts resolve to exactly $0 or $1 (no mid-value at settlement).

### Hype filter
A large fraction of "best Kalshi bot" results are SEO/affiliate content or paid managed bots (alphascope.app, tradingvps.io, quantvps.com/blog, **botforkalshi.com — $99/mo managed bot**). Prefer primary GitHub repos, `docs.kalshi.com`, the official SDK, and the Awesome list over ranked-list blogs (High confidence).

---

## 7. Risks, regulation, tax & scams

### Legality & geo (a bot MUST geofence by the trader's state of residence)
Kalshi is **federally legal as a CFTC-regulated DCM**; the **Third Circuit (April 7, 2026)** held its sports event contracts are "swaps" under the Commodity Exchange Act, so CEA preemption blocks state gaming law ([Holland & Knight](https://www.hklaw.com/en/insights/publications/2025/05/new-jersey-federal-court-sides-with-kalshi-over-prediction-market)). **But federal legality does not resolve the state question.** As of mid-2026 the map is shifting quarter-to-quarter: Nevada injunction (geofence deadline May 4, 2026), Ohio ~$5M fine, Arizona ~20 criminal misdemeanor charges (March 2026), Massachusetts geofence order (Jan 2026); Kalshi prevailed in NJ and Tennessee; the CFTC counter-sued Arizona, Connecticut, Illinois (April 2, 2026) ([Lines.com tracker](https://www.lines.com/guides/u-s-prediction-market-legal-status-state-by-state), Med confidence). **Drive geofencing from a maintained tracker, not a snapshot.**

### How algo traders lose money (the documented failure modes)
1. **The parabolic taker fee at mid-book** — 1.75¢/contract right where edges are thinnest and competition highest.
2. **Adverse selection on resting orders** — the 75% maker discount can be illusory; thin single-name markets get picked off by informed flow ([Bartlett, Stanford](https://law.stanford.edu/publications/adverse-selection-in-prediction-markets-evidence-from-kalshi/)). *Note: a prior co-author attribution ("Maureen O'Hara") is unconfirmed on the Stanford page — credit Bartlett alone.*
3. **Thin/patchy liquidity & legging risk** — Risk.net reports liquidity "too thin" for institutional use; cross-venue legs can leave naked directional exposure; Kalshi has had reliability incidents (lockouts during surges, stuck orders) acute for a bot assuming state consistency ([Risk.net](https://www.risk.net/markets/7963633/liquidity-on-kalshi-polymarket-%E2%80%98too-thin%E2%80%99-for-institutional-use)).
4. **Settlement carve-outs** — the **Feb 2026 Khamenei market** resolved at the pre-strike price via a "death carve-out" buried in contract terms rather than paying YES; a class action followed and Kalshi filed to codify a death-settlement rule ([DeFiRate](https://defirate.com/news/kalshi-codifies-death-settlement-rule-in-cftc-filing-amid-backlash-over-iran-market/)). **Parse resolution criteria programmatically per-market; exclude ambiguous ones** (High confidence).

### Surveillance — insider-trading enforcement is active (corrected figures)
Per [Al Jazeera (June 10, 2026)](https://www.aljazeera.com/economy/2026/6/10/prediction-platform-kalshi-to-collect-job-details-to-combat-insider-trading), in Q1 2026 Kalshi reported **150+ investigations launched, 100+ potential insider-trading cases blocked, 20+ law-enforcement referrals**, and is now collecting employment data to screen high-risk markets. (This supersedes the earlier "~200 investigations / a dozen cases" figure, which came from a lower-quality site.) **Implication: rely on public-data modeling and documented biases — not information-asymmetry plays that could be deemed insider trading. Abnormal one-sided flow can flag an account.**

### Tax (unsettled — settle classification before scaling churn)
- **No IRS guidance** on prediction-market event contracts. Kalshi issues *some* 1099s but **not a comprehensive 1099-B**; absence of a form doesn't remove the obligation — **log every fill/settlement for cost basis** ([Keeper](https://www.keepertax.com/posts/how-to-file-taxes-on-kalshi-and-polymarket), Med confidence).
- **Section 1256 (60/40) is an aggressive position with no clear authority** — event contracts aren't enumerated in §1256(b)(1), and "price movement alone does not satisfy the statutory daily mark-to-market requirement." **Do not model after-tax returns at the 60/40 rate as if settled** ([Camuso CPA](https://camusocpa.com/section-1256-prediction-market-tax/); [Green Trader Tax](https://greentradertax.com/prediction-market-taxes-capital-gains-gambling-or-something-else/), High confidence).
- **OBBBA 90% gambling-loss cap (effective tax year 2026, no carryforward)** can create **phantom taxable income** for a high-churn bot *if gains are classified as gambling*: $100k wins / $100k losses → only $90k deductible → $10k taxable on zero economic profit. The FAIR BET Act repeal has stalled. **The cap does not apply to capital-gains treatment** — a strong reason to settle classification with a CPA before scaling churn (High confidence).

### Scam patterns to avoid
The **[CFTC/SEC binary-options fraud advisory](https://www.cftc.gov/LearnAndProtect/AdvisoriesAndArticles/fraudadv_binaryoptions.html)** is the canonical red-flag list: refusal to credit/withdraw, identity-theft data harvesting, and software that manipulates prices/payouts. Core rule: *"If you cannot verify that they are registered, don't trade with them, don't give them money, and don't share your information."* Concrete red flags for prediction-market copy/signal services: **"risk-free arbitrage" marketing** (directly contradicted by documented partial-fill, settlement-divergence, and 0-32 weather failures), wash-traded "lucky-streak" wallets, brand-new wallets claiming high success, and any service requiring deposits on *its own* platform vs trading on regulated Kalshi. **Verifier note:** the "78% of arbitrage opportunities fail" stat traces only to secondary SEO/crypto blogs, not a primary study — treat the number as soft, though the qualitative claim (risk-free arb is overstated) is independently supported. Report fraud at cftc.gov/complaint.

---

## 8. Recommended build plan

**Target edge for v1: passive market-making capturing the maker discount in markets with persistent one-sided retail flow — *not* directional prediction, arbitrage, or weather.** This is the only edge with primary-research support (Becker, Bartlett, GWU/UCD), and it leans on Kalshi's biggest fee lever (maker/`post_only` ≈ $0 if held to settlement). Accept up front that the edge is thin and decaying, and that *adverse selection*, not fees, is the real adversary.

### Phase 1 — Plumbing (demo only)
1. Implement the **RSA-PSS signer** (timestamp_ms + METHOD + path-without-query, SHA-256, salt=digest length). Validate against the **demo** environment. Reuse `kalshi-starter-code-python` or `pykalshi` rather than hand-rolling the reconnection/retry layer.
2. Build **WebSocket book reconstruction** (snapshot + `orderbook_delta`), handling bids-only reciprocity and error code 25.
3. **Pull live fee params** (`/series/fee_changes`, `/margin/fee_tiers`) into a fee model — **never hardcode 0.07**; remember the 0.035 index half-price and that the per-category percentage table is *unverified*.
4. Fork **kapelame/kalshi-crypto-bot** as the scaffold (collector → backtester → paper → live, with the strategy stub) and **collect your own WebSocket data** (the official API gives no L2 history).

### Phase 2 — Backtest & paper-trade (this is non-negotiable)
5. Backtest the maker strategy on self-collected book data; model **fees rounded up per fill**, settlement to $0/$1, and — critically — **adverse selection** (fills cluster when fair value moved against you).
6. **Paper-trade live** for a meaningful sample before risking capital. The weather postmortem's lesson: a model's "90% certainty" was really ~75–80% once fat tails were honest.

### Phase 3 — Guardrails (implement before any live order)
- **Fee-aware position sizing:** every edge calc nets the *round-trip taker* or *maker-held-to-settlement* fee; reject trades whose modeled edge doesn't clear fees + spread + model error. Bias toward holding to settlement (avoids the exit fee).
- **Price-floor rule:** **never trade contracts below ~$0.15** (the 1¢ round-up makes them −EV as a % of price).
- **Liquidity checks:** require a minimum depth/spread before quoting; in thin single-name markets, widen quotes or skip (adverse-selection zone).
- **Kill-switches & limits:** `max_position_per_market`, `max_global_exposure`, `max_daily_loss`, a global kill switch, and a dry-run mode (the `ImMike/polymarket-arbitrage` controls are a good template).
- **Self-throttling:** respect the token buckets (Basic ≈ 10 writes/s); exponential backoff on 429 (no `Retry-After` header exists).
- **Resolution-criteria gate:** programmatically read each market's settlement source; **exclude ambiguous-resolution markets** (Khamenei carve-out lesson).
- **Geofence** by the trader's state of residence, driven by a maintained legal tracker.
- **State-consistency safety:** assume the API can lock out / drop state during surges; reconcile positions/fills on reconnect.

### Phase 4 — Scale carefully
- Graduate to live with **small size**; demo liquidity differs from production.
- Settle **tax classification with a CPA** before scaling churn (the OBBBA cap can manufacture phantom income).
- Only after a real maker track record, consider applying for the **Designated Liquidity Provider** program (signed MMA, auction-allocated) for a paid edge — but note it bars you from the general Fee Rebate Program.

**What NOT to build first:** a "risk-free" cross-venue arb bot (capital-fragmented, resolution-risk-dominated, the magnitudes are single-venue glitch stats), a weather/recurring-market directional bot (documented 0-32), or anything that out-forecasts macro markets (they already match professional forecasters). And **do not connect API keys or funds to any PR-launched "copy-trading" product** — Kalshi has no native copy-trading, so every such product is either mirroring Polymarket on-chain flow or is a low-trust funnel.

---

*Confidence summary: The fee math, API mechanics, and "naive directional loses" findings are High confidence and well-verified. The maker-taker structural edge is High confidence as a historical statistic but Medium as a retail-replicable strategy (adverse selection + decay). Cross-venue arb, news/calibration trades, and all secondary dollar magnitudes are Low-to-Medium. The refuted maker-fee-subset claim and the platform-mismatched "Polycule/Kalshi" scam claim have been corrected per the adversarial verdicts; the per-category fee table and "5¢ mispricing persists for days" are flagged unverified.*
