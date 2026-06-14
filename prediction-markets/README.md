# prediction-markets

Research subtree for **prediction-market** edges, isolated from the live Robinhood
cash-equities engine. **Research only — no capital, no live orders, no wallet.**
Scope and rules: see [`CLAUDE.md`](CLAUDE.md). Hypotheses + validation gates:
[`docs/polymarket-drift-v0-plan.md`](docs/polymarket-drift-v0-plan.md).

## Why a subfolder (not a separate repo) — for now
While this is pure research (scripts + notes, public read-only data) it reuses this
repo's backtest discipline and conventions at near-zero setup cost. It **graduates to
its own repo at Gate 3** (first funding / live execution), when separate secrets,
scheduler, kill switch, and git lifecycle actually become necessary.

## Venue: Kalshi (primary)
US-legal, CFTC-regulated, USD, no crypto wallet, clean public REST API
(`https://api.elections.kalshi.com/trade-api/v2`). Trading fees apply
(`≈ 0.07·p·(1−p)` per contract) but are smallest at the extremes — favorable for a
≥0.90 favorite strategy. (Polymarket is geo-restricted for the owner; kept as
reference only in `docs/`.)

## Current state
The "near-certain favorite" idea (buy the ≥0.90 side ending soon, hold to settlement)
is at the **coverage-probe** stage. `scripts/kalshi_pull.py` confirms the data is fully
accessible and reconstructs the price H hours before close from candlesticks using the
**ask** (real buy cost, not mid). Early small-sample read is *unfavorable*: near-resolution
extremes are Kalshi's best-calibrated region (little edge), and a single upset wipes many
small wins (negative skew). Not a verdict — sample is tiny.

## Scripts
```bash
# coverage/feasibility probe (public data, read-only)
python3 prediction-markets/scripts/kalshi_pull.py --horizon-hours 12 --threshold 0.90
python3 prediction-markets/scripts/kalshi_pull.py --series KXHIGHNY,KXBTCD --out prediction-markets/data/favorites.jsonl
```

## Next step
Scale `kalshi_pull.py` into the full **Gate 1 / H2** backtest: all liquid series × full
settled history × multiple horizons, with a liquidity floor, reporting the calibration
edge net of fees **and** the drop-top-N-winners stress test. Predicted outcome: clears
"data exists," fails "fee-survivable edge after the tail." If so, archive as a documented
no-edge result.
