# Autonomous Trading Engine — v0 Plan

Derived from `research/agentic-robinhood-mcp-landscape.md`. An **autonomous** momentum/DD engine on the
**official Robinhood Agentic MCP**, governed by code-level risk guardrails (no per-trade human approval).

## Operating decisions (locked in by the owner)
- **Funding**: a few thousand dollars incoming (next few days). Defaults below assume **~$3,000** — re-tune
  when the deposit lands and we read real buying power via `get_portfolio`.
- **Mandate**: this is the agent's **playground**. Full latitude to do DD, screen, and pick names — **no
  fixed watchlist**, no per-trade approval.
- **Autonomy**: **fully autonomous** execution. Safety = hard caps + daily circuit breaker + logging +
  Robinhood's kill switch (disconnect the MCP). Per-trade human sign-off is **off**.

## Design principles (the lessons, distilled)
- **Threshold must clear the spread.** JC Merlo ran 1% and regretted it; our F test spread was ~1.5%.
  **Rule: entry threshold ≥ 2× expected spread.** Fewer, higher-conviction trades.
- **Caps are the seatbelt, not a leash.** Free name selection, but bounded size/exposure/loss so a bug or a
  bad day can't blow up the stake.
- **Log everything, claim nothing unverified.** Every decision + fill to `data/`; P&L only from tool reads.

## Risk guardrails (`.env`-driven — see `.env.example`)
| Param | v0 default (~$3k) | Note |
|---|---|---|
| `TRADING_MODE` | `paper` → `live` when funded | sim until the deposit lands |
| `MAX_POSITION_USD` | `600` | ~20% per name |
| `MAX_TOTAL_EXPOSURE_USD` | `2400` | ~80% invested, keep cash buffer |
| `MAX_SYMBOL_WEIGHT` | `0.25` | concentration cap |
| `MAX_OPEN_POSITIONS` | `6` | diversification |
| `STOP_LOSS_PCT` / `TAKE_PROFIT_PCT` | `2.0` / `4.0` | per-position exits |
| `MAX_PER_TRADE_LOSS_USD` | `60` | absolute per-trade stop budget |
| `DAILY_MAX_LOSS_USD` | `150` | **circuit breaker** — halt new entries for the day |
| `SIGNAL_THRESHOLD_PCT` | `2.0` | entry trigger; ≥ 2× spread |

## DD / discovery (the "find stocks" half)
- **Universe per tick** = discovered, not fixed: liquid, fractional-eligible names with real intraday
  momentum. Sources: `search` for thematic baskets, plus a movers list (web/`MARKET_DATA_API_KEY`), filtered
  by `get_equity_tradability` (tradeable + fractional + regular-hours).
- **Conviction filter** before sizing: spread small enough, not halted, volume sane, move not already
  exhausted. Log the reasoning so picks are auditable.

## Engine loop (one tick, regular hours only)
1. `get_portfolio` → buying power, exposure; check **daily circuit breaker** — if tripped, manage exits only.
2. **Discover/refresh** candidate movers → `get_equity_tradability` filter.
3. `get_equity_quotes` → intraday % move + spread for candidates and open positions.
4. **Entries**: move ≥ `SIGNAL_THRESHOLD_PCT`, spread OK, within all caps → size (dollar-based) →
   `review_equity_order` (alert/log check) → `place_equity_order` (marketable limit). No human gate.
5. **Exits**: open positions hitting `TAKE_PROFIT_PCT` / `STOP_LOSS_PCT` (or `MAX_PER_TRADE_LOSS_USD`) →
   review → place.
6. Append every decision (incl. skips/no-trades) + fill to `data/engine-log.jsonl`; update daily P&L.

## Scheduling (how "autonomous" actually runs)
- Unattended: **`/schedule`** (cron) wakes the agent on a cadence during regular hours (9:30–16:00 ET) to run
  a tick; or a long-running **`/loop`** within a session. Either is our equivalent of JC Merlo's Task
  Scheduler — minus the babysitting, plus the caps.
- **Kill switch**: disconnect the `robinhood-trading` MCP, set `TRADING_MODE=paper`, or stop the schedule.

## Build order
1. `scripts/quote_snapshot.py` — read-only: pull candidate quotes + spreads + % moves. **Runnable now.**
2. `scripts/dd_screen.py` — discovery/conviction filter → ranked candidate list.
3. `scripts/engine.py` — the loop above; `paper` first, `live` on the flip. Append-only logging to `data/`.
4. Run in `paper` until funds land; review the log/P&L; tune caps + threshold; flip to `live`.

## Status / next
- ✅ Authorized autonomous mode; CLAUDE.md + settings + `.env` updated.
- ⏳ Waiting on the deposit (a few thousand). Until then: build the read-only DD + paper engine so we're ready
  to flip to `live` the moment funds + market-open coincide.
