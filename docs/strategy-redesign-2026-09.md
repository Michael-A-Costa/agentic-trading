# Strategy redesign v2 — BTC trend via ETF, stop-loss mandatory (PROPOSAL, 2026-09-23)

Status: **proposal, awaiting owner go-ahead.** Supersedes v1 (options-income core), which the
bake-off refuted. Evidence:
- `research/options-crypto-strategy-research-2026-09.md` — literature + forum sweep
- `data/bakeoff/options_results.md` — `scripts/bakeoff_options.py` (Cboe indexes, income ETFs, RH wheel replay)
- `data/bakeoff/crypto_results.md` — `scripts/bakeoff_crypto.py` (trend rules × 3 cost regimes, stop-loss study)

The old intraday momentum engine is **stopped** (launchd `trading-live`, `live-sentinel`,
`open-sweep-live` unloaded 2026-09-23).

## What the bake-off decided

**Options income — not the core.** Since 2007 every put-write / covered-call index had a *lower*
Sharpe than the S&P (PUT 0.46, BXM 0.38 vs 0.55), capturing ~50% of rallies but 60–80% of drops.
Income ETFs all trailed their underlying (JEPI 11.0% vs SPY 18.1%; bitcoin income ETFs 9–11% vs
IBIT 13–31%, still −44 to −51% drawdowns). The single-stock replay on real RH option marks: CSP-only
lost on IBIT (−10%) and SOFI (−40%); the SOFI wheel sat stuck below cost basis ~2.5 years. Stops on
short puts cut the worst trade but didn't create an edge. → Parked; optional dry-run learning only.

**Crypto venue — ETF shares, not coins.** RH crypto charges ~0.95% per side (~1.9% round trip);
Coinbase taker ~1.8%, post-only ~1.0%; IBIT/ETHA shares ~0.02% + 0.25%/yr. Trend rules only beat
buy-and-hold at ETF cost.

**Rule — BTC 50-day trend filter, volatility-targeted.** Out-of-sample (params chosen ≤2021, scored
2022→now) at ETF cost: 50d filter **+24.3%/yr**, vol-targeted **+23.6%/yr (Sharpe 1.01)** vs BTC
buy-and-hold +13.0% (0.50). Full history max drawdown ~−53 to −58% vs −84% holding.
Rejected: pullback-entry (last everywhere), top-coin rotation (−10%/yr OOS), ETH sleeve (parameter
choice fragile OOS — revisit later at small weight).

## What a good stop loss is (the owner's standing rule — every position has one)
1. **At invalidation, not a round number** — where the trade idea is wrong.
2. **Volatility-aware** — distance fits the asset's normal daily range.
3. **Sets position size** — `position = equity × risk_budget ÷ stop_distance`; dollar risk is fixed.
4. **Resting at the broker** whenever possible (survives our code/Claude being down). Whole shares only on RH; fractional → synthetic stop checked by our code.
5. **Gap-aware** — IBIT doesn't trade weekends/nights while BTC does; expect fills worse than the stop (measured worst: −10% vs an 8% stop, ≈1.3× the budget).
6. **Ratchets only** — tightened, never widened.
7. **Proven in backtest incl. whipsaw cost** — stops that sell the noise are rejected.

Applied: tight trailing/ATR stops were whipsawed 75–100% of the time and cut CAGR 20–55 pts → rejected.
Chosen: **fixed 8% below entry** — selected on pre-2022 data, neutral-to-slightly-positive after
(Sharpe +0.00–0.03), fires ~1.5×/yr. The 50-day exit remains the primary exit (worst trade −12% on
BTC without any stop); the 8% stop is the safety net for a crash between checks.

## The sleeve — rules (all in Python, no LLM discretion)
- **Signal**: BTC-USD daily close (Coinbase public candles — clean prices, not RH's marked-up crypto
  quote) vs its 50-day SMA. Above → long, below → flat.
- **Exposure**: vol-targeted to 50% annualized (30d realized vol), capped at 100% of the risk-sized
  position.
- **Instrument**: IBIT shares on the RH agentic account. Marketable limit orders, regular hours.
- **Stop**: 8% below entry, `stop_market` GTC resting on the whole-share lot; synthetic stop for any
  fractional remainder. Ratchet-only (never moved down).
- **Re-entry after a stop-out**: only on a fresh cross — BTC must close below the 50d SMA and then
  back above it.
- **Risk budget**: loss at the stop = **2% of equity** (owner to confirm). Position = 25% of equity.
- **Circuit breaker**: account equity −15% from its high-water mark → no new entries, flag owner.
- **Check-ins**: once daily ~09:45 ET (signal from the prior UTC close, act at the open) + ~15:50 ET
  (synthetic-stop check, reconcile). Nothing intraday; the resting stop covers the gaps between.
- **Logging**: every decision (incl. no-trade) → `data/engine-log.jsonl`; fills → `data/trades.jsonl`
  + daily blotter; reconcile vs broker `get_equity_orders` / `get_realized_pnl`.

## Funding
| Equity | Position at 2% risk | IBIT shares | Stop type |
|---|---|---|---|
| $100 | $25 | 0 whole | synthetic only |
| $2,500 | $625 | ~12 | resting GTC |
| $5,000 | $1,250 | ~25 | resting GTC |

At $100 the 2% rule allows no whole share. Two choices: (a) a **learning tier** with a raised budget
— 2 whole shares (~$98) with a resting 8% stop = ~$8 (8%) at risk, testing the full plumbing incl.
the resting stop; or (b) **$25 fractional** at the 2% budget with a synthetic stop. (Owner decides.)

## Proving it before scaling (pre-registered)
Live track vs the backtest: fills vs modeled open, stop fills vs modeled gap fills, signal days
matching the replay exactly. Scale beyond the learning tier after the plumbing has run ≥ 20 trading
days with zero reconciliation breaks; judge performance only over ≥ 1 full signal cycle (enter→exit),
never on weeks of P&L.

## Build order
1. `scripts/trend_sleeve.py` — pure signal + sizing + stop functions (unit-tested against
   `bakeoff_crypto.py` on history: same signal days).
2. Wire to the existing live path (`rh_mcp.py` review → place, ref_id idempotency, resting stop) behind
   `TREND_ARMED` (dry-run default).
3. launchd plist (09:45 + 15:50 ET), logs, reconcile; dry-run for a few sessions.
4. Owner flips `TREND_ARMED=1`. Update CLAUDE.md authorization/scope text for the new sleeve.

Options: optional zero-cost **dry-run journal** (real `review_option_order`, nothing placed) for
learning; no live options until something beats buy-and-hold in replay.
