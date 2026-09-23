# Options income & crypto trend — deep research (2026-09-23)

Automated multi-source research: 5 search angles, 22 sources fetched, 96 claims extracted, 25 checked 3 times each by independent fact-checkers (a claim was dropped when 2 of the 3 checks refuted it). 23 claims confirmed, 2 refuted, merged into 9 findings. Produced by the deep-research workflow, run wf_b78ad059-214.

## Question

How are retail traders and autonomous/AI-agent traders (2025–2026) actually making money with (a) options income strategies — the wheel, cash-secured puts, covered calls, poor-man's covered calls, credit spreads/iron condors, 0DTE/weekly premium selling, earnings IV-crush plays — and (b) long/directional options and (c) crypto trend-following or momentum — and which of these have evidence of a real, persistent edge vs. survivorship/hype? Sources to sweep: Reddit (r/thetagang, r/options, r/wallstreetbets, r/algotrading, r/CryptoCurrency), 4chan /biz/ (web search/fetch only — NO browser), X/Twitter, news articles, and quant/academic evidence (volatility risk premium, CBOE PUT/BXM index history, 0DTE retail P&L studies, earnings-straddle studies, crypto time-series momentum). Also cover anyone publicly running LLM/agent-driven options or crypto trading via the Robinhood MCP or similar broker APIs. CONSTRAINTS for the synthesis: the user's automated account is a Robinhood CASH account (can get options Level 2 = long calls/puts, covered calls, cash-secured puts; spreads need Level 3 which requires limited margin/margin), currently ~$100–200 but could be funded more; crypto spot is available (no crypto options on RH). Trader is a beginner at options and wants income-style plays that aren't too complicated, is OK with more risk and more frequent agent check-ins, but explicitly does NOT want another minute-by-minute intraday momentum scalper (their prior one showed no intraday edge). Deliver: ranked strategy candidates with evidence strength, failure modes (assignment, gap risk, early assignment, tail losses, liquidity/spreads, taxes/wash sales), capital needed per strategy at $200 / $2k / $10k / $25k tiers, sensible agent check-in cadence (daily / on-event) and mechanical rules (delta targets, DTE, profit-take %, roll/stop rules) that are backtestable, and what to measure to prove edge before scaling.

## Summary

The best-supported edge for this account is the volatility risk premium (VRP): options usually cost more than the volatility that follows. Cboe/Wilshire, AQR and Bondarenko data show VIX above later realized volatility in 20 of 21 years (1998-2018), averaging 19.3 vs 15.1 (1990-2018). Systematic index put-writing (Cboe PUT) roughly matched S&P 500 returns (about 9.5% vs 9.8%, 1986-2018) with about two-thirds of the volatility and a higher Sharpe. The cost is fat left tails: -26.8% in 2008, max drawdown -35.5%, and it lags in strong bull markets. The research that dates the 2008 and 2020 losses is from before those episodes or skips them. The opposite side is where retail loses: peer-reviewed and working-paper evidence shows retail option buyers lose on average, concentrated in short-dated calls, 0DTE and pre-earnings purchases. A large share of those losses is bid-ask spread (weeklys average 12.6% quoted, slightly-OTM options 28%). So the evidence ranks candidates this way: (1) a mechanical cash-secured-put / wheel on liquid, low-price underlyings, run as a daily-check VRP harvest; (2) a slow, long-or-cash crypto trend filter (50d MA, or a Donchian ensemble on top coins), which mainly cuts drawdowns rather than adding return; (3) no long/directional option buying, 0DTE or earnings-straddle buying as a core strategy. Important limits: none of the verified evidence tests a single-stock wheel in a small cash account. Unhedged CSPs carry almost full equity beta and do NOT inherit AQR's 0.68 Sharpe. At about $200 the wheel is barely feasible: one CSP needs 100 × strike in settled cash, which caps strikes near $2. It becomes practical around $2k (strikes of about $15-20) and diversifiable at $10k-$25k. So edge has to be proven with a paper/ledger replay before scaling, not taken on the strength of index studies.

## Findings

### 1. A persistent volatility risk premium exists in S&P 500 index options: implied volatility (VIX) has usually exceeded later realized volatility, and that gap is the source of option sellers' excess, risk-adjusted return, as distinct from their equity beta.

**Confidence:** high  ·  **Votes:** 3-0, 3-0, 2-1, 3-0 (merged claims 0, 1, 5, 7)

- VIX was above realized volatility in 20 of 21 years (1998-2018). The only exception was 2008, when the ratio was 89%.
- Average implied volatility was 19.3% against 15.1% realized (1990-2018; Bondarenko). OptionMetrics reports the premium still present in June 2026 (VRP about 5.6).
- Inside a BXM-style covered call (1996-2014), the short-vol component had a Sharpe near 1.0 and added about 2% a year while carrying under 10% of the risk. This held in all three subperiods.
- AQR's delta-hedged monthly 5% OTM put-selling strategy (1996-2016) earned 1.5% a year excess at 2.2% volatility: Sharpe 0.68, beta 0.04, max drawdown -10%.
- Caveat: VRP explains the excess return only. Most of a covered call's or put-write's total return and risk is equity beta.
- Caveat: AQR's Sharpe comes from daily delta-hedging with futures, which a Level-2 cash account cannot do. Its 1.5% a year is also a thin excess over cash.

Sources:
- https://cdn.cboe.com/resources/spx/wilshire-options-based-benchmark-indexes-2019.pdf
- https://www.cboe.com/insights/posts/white-paper-shows-volatility-risk-premium-facilitated-higher-risk-adjusted-returns-for-put-index/
- https://images.aqr.com/-/media/AQR/Documents/Insights/Journal-Article/Covered-Calls-Uncovered.pdf
- https://www.aqr.com/-/media/AQR/Documents/Whitepapers/Understanding-the-Volatility-Risk-Premium.pdf

### 2. Systematic cash-secured put selling on an index (Cboe PUT: monthly ATM SPX puts backed by T-bills) delivered about equity-like returns with lower volatility and a higher Sharpe over 32 years. It is the closest benchmark for a wheel or CSP program.

**Confidence:** high  ·  **Votes:** 3-0, 2-1 (merged claims 4, 8)

- June 1986-Dec 2018: 9.54% vs 9.80% annualized, volatility 9.9% vs 14.9%, Sharpe 0.64 vs 0.45, beta 0.56, monthly alpha about 0.2%.
- PUT ranked first against five comparison indexes on Sharpe, Sortino and Stutzer.
- Caveat: it lagged in raw return in strong bull years: 2020 +2.1% vs +18.4%, 2023 +14.3% vs +26.3%, 2024 +17.8% vs +25.0%. It fell less in 2022 (-7.7% vs -18.1%).
- Caveat: Cboe sponsors the index. Results are gross of costs and taxes. The underlying is SPX (European, cash-settled, no early assignment), not single stocks.

Sources:
- https://cdn.cboe.com/resources/spx/wilshire-options-based-benchmark-indexes-2019.pdf
- https://www.cboe.com/insights/posts/white-paper-shows-volatility-risk-premium-facilitated-higher-risk-adjusted-returns-for-put-index/

### 3. Short-option income strategies carry real tail risk. Large moves in either direction hurt, and drawdowns cluster with equity crashes, so 'income' strategies can still draw down 25-35%.

**Confidence:** high  ·  **Votes:** 3-0, 3-0 (merged claims 3, 6)

- AQR's beta-hedged short-vol strategy lost in March 2000 (S&P +~10%) and September 2008 (S&P -~10%).
- Option-selling indexes have negative skew (-1.11 to -2.10 vs -0.81 for the S&P 500) and high kurtosis (up to 9.72 vs 2.53).
- Their max drawdowns ran -35.5% to -42.7% vs -50.95% for the S&P 500, only 16-30% smaller.
- 2008: PUT -26.8%, BXMD -31.3%, S&P -37.0%.
- PUT's Sortino (0.43) was about equal to the S&P's (0.42), so the Sharpe advantage overstates how much safer it is.
- For a single-stock wheel the tail is worse. Idiosyncratic gaps (earnings, fraud, guidance) cause assignment far below the strike, and the agent then holds stock that keeps falling.

Sources:
- https://www.aqr.com/-/media/AQR/Documents/Whitepapers/Understanding-the-Volatility-Risk-Premium.pdf
- https://cdn.cboe.com/resources/spx/wilshire-options-based-benchmark-indexes-2019.pdf

### 4. Systematically buying options is negative expected value. This covers index protective puts and the typical retail short-dated long call or put, and it argues against long/directional options as a core strategy.

**Confidence:** high  ·  **Votes:** 3-0, 3-0 (merged claims 2, 12)

- Rolling 1-month 5% OTM protective puts (1996-2016) cut return from 5.1% to 1.8% and Sharpe from 0.32 to 0.14, while max drawdown only improved from 62% to 57%.
- Retail options traders lost about $1.2B in aggregate (10-day horizon, Nov 2019-Jun 2021; Journal of Finance 2023). Losses were concentrated in short-term calls, and costs add about $5.2B more.
- Caveat: the retail sample is identified by a proxy (SLIM trades) and comes from the meme era.

Sources:
- https://www.aqr.com/-/media/AQR/Documents/Whitepapers/Understanding-the-Volatility-Risk-Premium.pdf
- https://www.jbs.cam.ac.uk/wp-content/uploads/2022/06/2022-ccaf-conference-version-paper-bryzgalova-pavlova-sikorskaya-final.pdf

### 5. 0DTE and weekly option trading by retail loses money on average, and spreads and fees drive most of the loss. Losses come mainly from single-leg debit (premium-buying) trades in high-IV contracts; credit/seller-side trades do relatively better.

**Confidence:** medium  ·  **Votes:** 3-0, 3-0, 3-0, 3-0 (merged claims 9, 10, 11, 13)

- Retail lost about $241k/day on 0DTE SPX (Feb 2021-Sep 2023), rising to about $350k/day after daily expirations began in May 2022. The total was over $125M, of which more than $90M was transaction costs (about 60-72% depending on the measure).
- Debit trades averaged -$3.64k/day vs +$1.22k/day for credit trades.
- About 50% of retail trades are in weeklys, with a 12.6% quoted spread (about 6.6% effective). Slightly-OTM options average 28% quoted and micro trades (<=$250) 23.6%.
- Disputed: Cboe-funded papers argue the retail proxy is unrepresentative, and one finds SLIM customer trades profitable.
- Implication for a $200-$2k account: spread cost alone can erase any edge on cheap weekly contracts. Trade only tight-spread chains and use mid-price limit orders.

Sources:
- https://papers.ssrn.com/sol3/Delivery.cfm/4404704.pdf?abstractid=4404704&mirid=1
- https://www.jbs.cam.ac.uk/wp-content/uploads/2022/06/2022-ccaf-conference-version-paper-bryzgalova-pavlova-sikorskaya-final.pdf

### 6. Retail buys options heavily before earnings and overpays relative to realized volatility. The earnings IV-crush premium exists, but market makers capture it; the evidence does not show that a small retail seller can.

**Confidence:** medium  ·  **Votes:** 3-0 (claim 14); related claim refuted 0-3

- Review of Finance (2025), de Silva, Smith & So: retail loses 5-9% on earnings-option investments, 10-14% for high expected-abnormal-volatility (EAV) events, about $3B in total.
- Straddles after high-EAV announcements underperform by a further 9% over two weeks (t=8.26).
- Caveats: data is 2010-2021. Part of the loss is spread and disposition effect.
- A companion claim that 'the short side is where the edge sits' for retail was REFUTED 0-3. Selling earnings premium in a cash account means paying the spread and eating gap/jump risk.
- Treat an earnings IV-crush strategy as unproven: at most a small, separately measured sleeve (e.g., CSPs well outside the implied move on large caps), never the core.

Sources:
- https://www.timdesilva.me/files/papers/losing_optional.pdf

### 7. Crypto time-series trend-following (long-or-cash) has consistent backtest support, including on survivorship-bias-free data. The benefit is mainly lower drawdowns and volatility (better Sharpe), not reliably higher total return, and published figures are in-sample and vendor-produced.

**Confidence:** medium  ·  **Votes:** 3-0 x5 (merged claims 15, 16, 17, 18, 22)

- Concretum (2025): a Donchian multi-lookback ensemble with volatility sizing, rotating across the top-20 liquid coins, reported net-of-fees Sharpe >1.5, alpha 10.8% vs BTC and CAGR about 30%. Data covers all coins since 2015, including dead ones.
- That coin universe still requires one year of listing and $2M volume, so some survivorship remains.
- Grayscale (2012-Jul 2023): BTC 50d MA long/cash had Sharpe 1.9 vs 1.3 buy-and-hold; a 20/100 crossover returned 116% vs 110% with Sharpe 1.7. In 2020-2023 the MA strategies had lower total return than buy-and-hold.
- The arXiv hourly SMA result (73,700%) assumes zero costs and is an upper bound only.
- This is time-series trend, not the cross-sectional momentum that failed in this repo. It is not peer-reviewed, and its Sharpes are in-sample and should be expected to decay.

Sources:
- https://papers.ssrn.com/sol3/Delivery.cfm/5209907.pdf?abstractid=5209907&mirid=1
- https://research.grayscale.com/reports/the-trend-is-your-friend-managing-bitcoins-volatility-with-momentum-signals
- https://arxiv.org/pdf/2009.12155

### 8. On the Robinhood agent platform, orders are confined to the dedicated Agentic account. Crypto needs a linked Robinhood Crypto account, is unavailable in some states (e.g., NY), and agents cannot transfer, stake or lend it. The live MCP exposes option order tools even though the overview page lists only equities and crypto, so options access depends on the account's options level.

**Confidence:** high  ·  **Votes:** 3-0 x3 (merged claims 19, 20, 21)

- The support page was fetched 2026-09-23: 'Your agent can only place trades in your Robinhood Agentic account'; 'Crypto trading through your agent isn't available in every state, including New York'; 'can't transfer, stake, or lend it.'
- The session's MCP includes place_option_order, review_option_order, exercise_option, get_option_chains/quotes/positions, get_option_level_upgrade_info and get_limited_margin_upgrade_info, plus place_crypto_order and preview_crypto_order.
- On a cash account, Level 2 allows CSPs, covered calls and long options. Spreads and iron condors need Level 3, which requires margin.

Sources:
- https://robinhood.com/us/en/support/articles/agentic-trading-overview/

### 9. Synthesized strategy ranking and mechanical plan for this account. These are design proposals built from the evidence above; the specific parameters have NOT been verified.

**Confidence:** low  ·  **Votes:** n/a (synthesis)

**Ranked candidates**
1. Mechanical wheel (CSP, then covered call on assignment) on liquid ETFs or large caps with tight option spreads. VRP-backed; beginner-friendly; fits Level 2 on a cash account.
2. Crypto long/cash trend filter (BTC/ETH 50d MA, or a small Donchian ensemble), checked daily. Evidence is medium; the benefit is drawdown control.
3. Covered calls on existing equity holdings (VRP, small add-on).
4. Earnings or 0DTE premium selling and all long-option directional bets: excluded from the core; evidence is negative or unproven.

**Candidate rules to backtest (common practitioner defaults, not verified here)**
- CSP: 30-45 DTE, 0.20-0.30 delta.
- Take profit at 50% of credit, or roll at about 21 DTE.
- Skip if an earnings date falls before expiry, or if the bid-ask spread is over about 5-10% of mid.
- Take assignment, then sell a covered call at about 0.30 delta with the strike at or above cost basis.
- Cap: at most one underlying at 20-25% of equity, with a sector limit.
- Hard exit if the underlying falls more than X% below the strike. The threshold is to be backtested; the alternative is simply accepting the assignment.

**Capital tiers**
- $200: no practical CSP (strike ≤ $2 means junk liquidity). Use crypto trend or fractional equity only, or paper-trade the wheel.
- $2k: one CSP at a ≤$20 strike, zero diversification.
- $10k: 3-5 underlyings.
- $25k: a diversified wheel. Consider the Level 3 / limited-margin upgrade for defined-risk put spreads, which cut capital needed per position.

**Check-in cadence**
- Once daily after the open plus a pre-close check.
- Event triggers: underlying down more than 1.5-2× the implied move, profit target hit, 21 DTE reached, early-assignment risk (short call ITM before ex-dividend), or earnings approaching.
- No minute-level ticking.

**Failure modes**
- Assignment gap-downs.
- Early assignment of short calls around ex-dividend.
- Wide spreads on cheap chains.
- Cash-account T+1 settlement and good-faith violations (GFVs) when recycling assignment and sale proceeds.
- Short-term capital gains tax and wash sales when re-selling puts on a name just sold at a loss.
- Capped upside that lags buy-and-hold in bull markets.

Sources:
- https://cdn.cboe.com/resources/spx/wilshire-options-based-benchmark-indexes-2019.pdf
- https://www.aqr.com/-/media/AQR/Documents/Whitepapers/Understanding-the-Volatility-Risk-Premium.pdf
- https://papers.ssrn.com/sol3/Delivery.cfm/4404704.pdf?abstractid=4404704&mirid=1
- https://research.grayscale.com/reports/the-trend-is-your-friend-managing-bitcoins-volatility-with-momentum-signals

## Caveats

- **Dated evidence.** Most option evidence is SPX index data ending 2014-2018 (AQR, Cboe/Wilshire). It excludes or predates the Feb 2018 volatility blowup, the Mar 2020 crash and the 2022+ 0DTE boom, apart from partial PUT calendar-year figures through 2024.
- **Wrong instrument.** Index put-writing is European, cash-settled and diversified. A single-stock wheel in a cash account has early-assignment, idiosyncratic-gap and liquidity risks the index studies never tested.
- **Hedging mismatch.** AQR's high Sharpe comes from daily-delta-hedged strategies, which are not reproducible at Level 2.
- **Interested sources.**
  - Cboe sponsors the PUT/BXM research.
  - Grayscale and Concretum have commercial interests, and their crypto results are in-sample backtests, not live P&L. Grayscale's parameters may have been chosen after the fact.
  - The 0DTE retail-loss study is an unreviewed working paper built on a contested retail proxy, disputed by Cboe-funded rebuttals. Its $90M/$125M and 60% figures come from different measures and versions.
- **Research not covered by verified claims.** The requested Reddit, 4chan /biz/ and X sweep produced no claims that survived verification. Nothing verified describes how practitioners on those forums actually perform, and no verified public track record exists of LLM agents trading options or crypto via the Robinhood MCP. Forum anecdotes should be treated as survivorship-biased.
- **Unverified parameters.** Specific mechanical parameters (delta, DTE, 50% take-profit, 21-DTE roll) are conventional practitioner defaults and were not among the verified claims.
- **Taxes and settlement.** Wash-sale and short-term gains effects, and T+1 settlement and GFV interactions in this cash account, were not quantified by any source.
- **Account size.** At the current ~$100-200, a meaningful CSP program is infeasible, so any live options test requires more funding or starts as paper replay.

## Open questions

- What do real-world single-stock wheel/CSP returns look like after spreads, assignment and taxes on liquid $10-50 underlyings from 2019 to 2026? Can this be replayed from RH get_option_historicals plus equity historicals before committing capital?
- Is the VRP on individual liquid stocks and ETFs (net of the 5-10% retail spreads on cheaper chains) large enough to survive costs at $2k-$10k scale, or does it only pay on index/SPY-scale liquidity?
- Does a crypto 50d-MA or Donchian trend filter keep any edge out-of-sample in the 2024-2026 ETF era once fees, spread and taxable churn in the RH crypto account are included?
- Is the Level 3 / limited-margin upgrade (defined-risk put spreads) worth its GFV and settlement tradeoffs versus staying cash-only Level 2, and at what account size does it pay off?

## Refuted claims

- (0-3) Most of a covered call's risk and return comes from simply holding the equity. The premium is a small add-on, and 'income' is not a separate source of edge. — https://images.aqr.com/-/media/AQR/Documents/Insights/Journal-Article/Covered-Calls-Uncovered.pdf
- (0-3) Retail option buyers around earnings announcements lose 5-9% on average, and 10-14% for high expected-announcement-volatility (EAV) events. This means buying pre-earnings long options/straddles is a negative-expectancy retail behavior, and the counterparty (short premium) side is where the edge sits. — https://www.timdesilva.me/files/papers/losing_optional.pdf

## Sources

- [primary] https://images.aqr.com/-/media/AQR/Documents/Insights/Journal-Article/Covered-Calls-Uncovered.pdf — Academic: options premium-selling edge (5 claims)
- [primary] https://www.aqr.com/-/media/AQR/Documents/Whitepapers/Understanding-the-Volatility-Risk-Premium.pdf — Academic: options premium-selling edge (5 claims)
- [primary] https://cdn.cboe.com/resources/spx/wilshire-options-based-benchmark-indexes-2019.pdf — Academic: options premium-selling edge (5 claims)
- [primary] https://www.cboe.com/insights/posts/white-paper-shows-volatility-risk-premium-facilitated-higher-risk-adjusted-returns-for-put-index/ — Academic: options premium-selling edge (5 claims)
- [primary] https://www.cboe.com/insights/posts/index-insights-march-2026 — Academic: options premium-selling edge (3 claims)
- [unreliable] https://www.academia.edu/16327015/EQUILIBRIUM_INDEX_AND_SINGLE_STOCK_VOLATILITY_RISK_PREMIA — Academic: options premium-selling edge (0 claims)
- [blog] https://earlyretirementnow.com/2024/09/17/the-wheel-strategy-doesnt-work-options-series-part-12/ — Practitioner mechanics: the wheel on a small account (5 claims)
- [blog] https://apexvol.com/strategies/wheel-strategy/backtest — Practitioner mechanics: the wheel on a small account (5 claims)
- [blog] https://premiumstrike.app/blog/best-dte-for-wheel-strategy — Practitioner mechanics: the wheel on a small account (5 claims)
- [primary] https://papers.ssrn.com/sol3/Delivery.cfm/4404704.pdf?abstractid=4404704&mirid=1 — Contrarian: retail options losses and survivorship (5 claims)
- [primary] https://www.jbs.cam.ac.uk/wp-content/uploads/2022/06/2022-ccaf-conference-version-paper-bryzgalova-pavlova-sikorskaya-final.pdf — Contrarian: retail options losses and survivorship (5 claims)
- [primary] https://www.timdesilva.me/files/papers/losing_optional.pdf — Contrarian: retail options losses and survivorship (5 claims)
- [primary] https://papers.ssrn.com/sol3/Delivery.cfm/5209907.pdf?abstractid=5209907&mirid=1 — Crypto trend-following evidence (5 claims)
- [secondary] https://arxiv.org/pdf/2009.12155 — Crypto trend-following evidence (5 claims)
- [primary] https://research.grayscale.com/reports/the-trend-is-your-friend-managing-bitcoins-volatility-with-momentum-signals — Crypto trend-following evidence (5 claims)
- [blog] https://papers.ssrn.com/sol3/Delivery.cfm/4955617.pdf?abstractid=4955617&mirid=1 — Crypto trend-following evidence (4 claims)
- [primary] https://robinhood.com/us/en/support/articles/agentic-trading-overview/ — AI-agent trading via broker APIs and MCP (5 claims)
- [secondary] https://genfinity.io/2026/07/21/robinhood-agentic-trading-crypto-ai-agents/ — AI-agent trading via broker APIs and MCP (5 claims)
- [blog] https://github.com/guntoken/alpaca-wheel-agent — AI-agent trading via broker APIs and MCP (5 claims)
- [unreliable] https://nof1.ai/ — AI-agent trading via broker APIs and MCP (0 claims)
- [blog] https://alpaca.markets/learn/building-a-multi-agent-ai-trading-system-on-alpaca — AI-agent trading via broker APIs and MCP (4 claims)
- [blog] https://ryandoser.com/ai-trading-agent-robinhood/ — AI-agent trading via broker APIs and MCP (5 claims)

## Live chain snapshot (RH MCP, 2026-09-22 close, ~0.25-delta puts, 30 Oct expiry, 37 DTE)

Not part of the verified research. These are live quotes pulled after the research finished, to ground capital needs.

| Symbol | Spot | Strike | Collateral | Mid ann. yield | Spread % | OI | IV | Earnings before expiry |
|---|---|---|---|---|---|---|---|---|
| NIO | 3.71 | 3.5 | $350 | 31% | 18% | 1150 | 44% | no (11/24) |
| F | 13.10 | 12 | $1,200 | 16% | 10% | 119 | 38% | yes 10/22 |
| SOFI | 17.16 | 15.5 | $1,550 | 26% | 5% | 1316 | 51% | yes 10/27 |
| T | 25.12 | 23.5 | $2,350 | 15% | 36% | 39 | 32% | yes 10/21 |
| PFE | 27.93 | 26.5 | $2,650 | 10% | 12% | 83 | 23% | no (11/3) |
| IBIT | 48.83 | 45.5 | $4,550 | 22% | 2% | 591 | 39% | n/a |
| BAC | 56.21 | 53 | $5,300 | 13% | 13% | 27 | 28% | yes 10/14 |
| SLV | 60.74 | 56.5 | $5,650 | 20% | 6% | 134 | 38% | n/a |
| KO | 88.61 | 85 | $8,500 | 10% | 18% | 121 | 21% | yes 10/20 |
| INTC | 123.84 | 109 | $10,900 | 46% | 4% | 49 | 74% | yes 10/22 |
| HOOD | 124.20 | 111 | $11,100 | 39% | 6% | 97 | 65% | no (11/4) |
| PLTR | 184.99 | 170 | $17,000 | 28% | 5% | 537 | 48% | no (11/2) |
| IWM | 287.20 | 277 | $27,700 | 10% | 1% | 384 | 19% | n/a |

## Practitioner / forum sweep (ANECDOTAL — not adversarially verified)

Separate pass after the verified research. **reddit.com and the 4chan archives (archived.moe, warosu) blocked direct fetching**, so the forum parts rely on search-engine snippets and aggregator sites. Tags: [primary] fetched source · [backtest-vendor] simulated, unaudited · [anecdotal] secondhand.

### Wheel/CSP practice (r/thetagang, r/options, r/Optionswheel, via aggregators)
- The rules people converge on:
  - sell puts at ~30 delta (about 30% odds of finishing in the money), 7–35 days to expiry
  - take profit at 50% of the premium
  - when rolling a position to a later expiry, only do it if you're paid more for the new option than you pay to close the old one
  - "only sell puts on stocks you'd genuinely own at that strike"

  [anecdotal] — riskpicks.com, optionwheellogic.com
- Tickers commonly used by small accounts: SOFI, F, PLTR, AMD, INTC, SPY/QQQ/IWM. Meme names (GME/AMC/BB/BBBY) are explicitly called "wheel poison". [anecdotal] — apexvol.com/best/stocks-for-wheel-strategy
- How accounts blow up:
  - rolling a losing put down and out forever
  - getting assigned in 2022 and holding the shares through the drop, then selling covered calls below cost basis
  - chasing high-premium meme or earnings names

  [anecdotal] — fattail.ai/wheel-strategy-options

### The critical view
- **earlyretirementnow, "The Wheel Strategy Doesn't Work" (2024-09-17)** [primary]:
  - In bear markets (avg 1.3 yr, 3.6 yr to recover) the wheel ends up selling calls far above a depressed cost basis, so it earns almost nothing for years.
  - Leveraged wheeling gets wiped out by a 30% drawdown.
  - How much market exposure you carry depends on your assignment history, which reflects loss aversion rather than a strategy.
  - It's a disguised stock-picking and timing bet.
  - Promoters report realized premium while hiding unrealized losses on assigned shares ("Enron accounting").
- **ApexVol wheel backtest 2020–2024** [backtest-vendor, simulated]. 25-delta puts, 72% win rate, compared with just holding the stock:

  | Underlying | Wheel return | Buy-and-hold | Max drawdown |
  |---|---|---|---|
  | SPY | +41% | +85% | −13% |
  | AAPL | +62% | +128% | −24% |
  | KO | +27% | +18% | −8% |

  It beat SPY by about 17 points in 2022. The wheel wins in flat or down markets and lags badly in rallies.
- compoundinglab substack and fattail.ai [blog]:
  - Capped upside plus tax drag at short-term rates (about 1–3%/yr) make the wheel lag SPY over full market cycles.
  - It wins small and loses big: a 30% drawdown erases about 10 months of premium.

### 4chan /biz/ and X [low quality, secondhand only]
- Could not fetch any 4chan content. Themes reported secondhand:
  - BTC dollar-cost averaging with a trend or dominance filter, rotating 10–20% into altcoins when BTC dominance falls
  - MSTR as a leveraged BTC proxy
- No substantive strategy content found. Price prophecies are noise.

### Public agents on the Robinhood agentic MCP
- Robinhood Agentic Trading launched 2026-05-27 with equities and options; crypto was added around 2026-07-21 [primary]. Sources: techcrunch.com/2026/05/27/robinhood-now-lets-your-ai-agents-trade-stocks/ and genfinity.io/2026/07/21/robinhood-agentic-trading-crypto-ai-agents/
- NexusTrade review (2026-07-08, written by a competitor) [anecdotal]:
  - No multi-leg options and no paper-trading sandbox.
  - Investor-profile re-verification gets in the way.
  - The OAuth redirect only works on localhost.

  nexustrade.io/blog/robinhood-agentic-trading-mcp-review-20260708
- **github.com/guntoken/alpaca-wheel-agent** (on Alpaca, not RH) [primary]:
  - A deterministic engine sells puts at ~30 delta, 7–35 days to expiry. It takes profit at 50%, rolls only when the roll pays a net credit, and sells covered calls after assignment.
  - Claude only has a veto ("risk governor").
  - Guardrail limits:

    | Rule | Limit |
    |---|---|
    | Cash backing one stock's puts | ≤ 18% of the account |
    | Cash backing all puts combined | ≤ 72% (halved in weak markets) |
    | Delta | 0.18–0.42 |
    | Open interest | ≥ 200 |
    | Buy/sell gap | ≤ 15% |
    | Implied-volatility percentile | ≥ 40th |
    | Daily loss | halt at −3% |

    It also has a kill-switch file and runs as a dry run by default.
  - Self-reported, paper-only results: a backtest showing +27% vs SPY +20% over 1 year and +55% vs +46% over 2.5 years, with a 21.5% max drawdown. Unaudited.

### IBIT (spot-bitcoin ETF) options as the crypto income vehicle
- IBIT options have been listed since Oct 2024. By 2026 they're described as extremely liquid near the current price, with $0.01–0.03 buy/sell gaps. Our own chain snapshot above shows a 2% gap on a $45.50 put. [primary/secondary] Sources: barchart.com/etfs-funds/quotes/IBIT/covered-calls and crypto.news
- Wrapped products (Grayscale BTCC, Roundhill YBTC) show demand for this. No forum data shows how many people "wheel IBIT" directly.

### Consensus rules / top blow-up modes (synthesized from this sweep)
- **Rules:**
  - Only sell puts on names you'd own.
  - Delta 20–35, 2–5 weeks to expiry, take profit at 50%.
  - Roll only when the roll pays a net credit.
  - Cap how much cash backs one stock's puts, and all puts combined.
  - Stick to boring, liquid names.
  - Pre-define the exit.
- **Blow-ups:**
  1. rolling down forever
  2. holding assigned shares through the drop, with covered calls sold below cost basis
  3. high-premium meme or earnings names
  4. concentration
  5. counting only realized premium while ignoring unrealized losses on assigned shares
