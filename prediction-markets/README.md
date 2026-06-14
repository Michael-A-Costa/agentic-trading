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

## Current state — IDEA TESTED, ARCHIVED (no edge)
The "near-certain favorite" idea (buy the ≥0.90 side ending soon, hold to settlement) was
swept across the **top 50 liquid series, n=1,655 bets, 24h horizon** and **fails decisively**:
favorites are ~3.3¢ **over**priced (realized win 0.927 vs implied 0.960), mean net return
**−3.78%/bet**, t=−5.66, and the loss is **robust to the drop-top-N stress test** (i.e. broad
and systematic, not a single-upset artifact). Full write-up:
[`docs/kalshi-near-certain-favorite-FINDINGS.md`](docs/kalshi-near-certain-favorite-FINDINGS.md).
This is Gate 1 / H2 = FAIL → archived. The tooling (`scripts/kalshi_pull.py`) stands for
any future prediction-market hypothesis.

## Scripts
```bash
# coverage/feasibility probe (public data, read-only)
python3 prediction-markets/scripts/kalshi_pull.py --horizon-hours 12 --threshold 0.90
python3 prediction-markets/scripts/kalshi_pull.py --series KXHIGHNY,KXBTCD --out prediction-markets/data/favorites.jsonl
```

## Next step
None required for this idea — it's archived. The discovery cache
(`data/kalshi_series_liquid.json`) and tooling remain for the next hypothesis (e.g.
post-news drift / H1, or a different price band). Re-runs skip discovery.
