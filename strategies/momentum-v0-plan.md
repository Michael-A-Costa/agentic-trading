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
| `DAILY_MAX_LOSS_USD` | `150` | **circuit breaker** — halt new entries (re-checked at fill; exits still run) |
| `SIGNAL_THRESHOLD_PCT` | `2.0` | absolute intraday entry trigger; ≥ 2× spread |
| `REL_STRENGTH_PCT` | `1.0` | also require this much intraday % **above SPY** (don't just buy beta) |
| `MIN_POSITION_USD` | `0` | reject dust fills (0 = off) |
| `COOLDOWN_MIN` | `30` | no re-entry into a name within N min of exiting (anti-whipsaw) |
| `FLATTEN_BEFORE_CLOSE_MIN` / `NO_ENTRY_LAST_MIN` | `15` / `15` | EOD flatten + block late entries |
| `MAX_HOLD_MIN` | `0` | force-exit a stale position (0 = off) |
| `DD_MODEL` / `MAX_DD_CANDIDATES` / `DD_CACHE_TTL_MIN` | Sonnet / `2` / `180` | Stage-2 commit model + cost bounds |

> **All caps above are enforced deterministically** in `apply_decision.py` (buy branch) and
> `tick_context.py` — `MAX_SYMBOL_WEIGHT` (fraction of live equity) and `MAX_PER_TRADE_LOSS_USD`
> (bounds size so `notional × STOP_LOSS_PCT ≤` budget) are real reject branches, not just config.

### Stop protection: synthetic now, resting-broker later
Every buy attaches an explicit `stop_price` (−`STOP_LOSS_PCT`) and `take_profit_price`
(+`TAKE_PROFIT_PCT`), tagged `stop_type: "synthetic"`. **Synthetic = our engine sells when it
checks the level at tick time (~5 min) and the host is awake.** It is *not* a resting broker
order, so it does **not** cover between-tick moves, overnight/pre-market gaps, or a crashed/asleep
engine. Logs say "synthetic stop hit" so this is never mistaken for broker-grade protection.

- **Paper / now:** keep synthetic. Fractional sizing stays; agent is always free to sell; no
  open-order lock to manage. Good enough to validate the strategy.
- **Live, later (not whole-share-by-default):** Robinhood resting stops need **whole shares**, and
  fractional is market-only — so a real resting stop can only ride on whole-share-eligible lots. Plan
  is a **hybrid**: real resting **stop-market** (not stop-limit — a limit can gap through and never
  fill) when the lot is whole-share-eligible, synthetic otherwise. Keep fractional sizing as the
  default; never force whole-share trading just to get a resting stop. Hybrid adds: read
  `get_equity_orders` each tick (know the resting stop + its id), **cancel-stop → sell** for
  discretionary exits, and reconcile the "stop already fired while asleep" case (position gone →
  book realized P&L, don't re-sell).

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

## Scheduling (how "autonomous" actually runs) — headless MCP VERIFIED
- **Headless `claude -p` reaches the MCP** (probed 2026-06-04: `get_accounts` returned 4 accounts from a
  non-interactive process, no browser auth — see `scripts/README.md`). So the unattended path is real:
  **OS cron → `claude -p` in this repo → MCP tools**, scoped by `--allowedTools` + `.env` caps.
- Alternatives: a long-running **`/loop`** within a session, or **`/schedule`** for a managed routine.
- This is our equivalent of JC Merlo's Windows Task Scheduler — minus the babysitting, plus the caps.
- **Kill switch**: disconnect the `robinhood-trading` MCP, set `TRADING_MODE=paper`, drop the write tools
  from `--allowedTools`, or stop the cron/schedule.

## Build order
1. `scripts/quote_snapshot.py` — read-only: pull candidate quotes + spreads + % moves. **Runnable now.**
2. `scripts/dd_screen.py` — discovery/conviction filter → ranked candidate list.
3. `scripts/engine.py` — the loop above; `paper` first, `live` on the flip. Append-only logging to `data/`.
4. Run in `paper` until funds land; review the log/P&L; tune caps + threshold; flip to `live`.

## Status / next
- ✅ Authorized autonomous mode; CLAUDE.md + settings + `.env` updated.
- ✅ Paper engine live (deterministic gather/screen/exits + Stage-2 LLM DD commit), logging to `data/`.
- ⏳ Waiting on the deposit (a few thousand). Until then: run `paper`, review the log/P&L, tune knobs,
  then build the live `review → place` path and flip to `live`.

## Engine review — hardening applied (2026-06-04)
A multi-dimension review (safety / algo / prompts / flows / code / docs) drove these fixes:
- **Guardrails now real:** `MAX_SYMBOL_WEIGHT` + `MAX_PER_TRADE_LOSS_USD` enforced at fill (were
  dead config); daily circuit breaker re-checked at fill (not just the gate); NaN/inf sizes and
  dust (`MIN_POSITION_USD`) rejected; exposure valued at `max(last, entry)` (never under-counts).
- **Risk inversion fixed:** on a `circuit_breaker` SKIP the engine still runs **protective exits**
  (it halts entries, not stops) — but stays idle on stale/closed-market SKIPs.
- **Atomic state writes** + refuse-on-corrupt-state (no silent re-baseline of the breaker).
- **Live-mode fail-closed:** the wrapper and executor refuse `TRADING_MODE=live` until a real
  `review → place` path exists.
- **Flow:** a DD model timeout/parse-failure is now `error` (retried next tick), never a cached
  "reject"; a `commit` without a valid size downgrades to reject; Stage-2 is portfolio-aware
  (headroom, open positions, held names) and gets `range_pos` / relative strength.
- **Algo:** relative-strength-vs-SPY screen, post-exit cooldown, EOD-flatten / max-hold time exits,
  `extended` flag no longer vetoes volume-backed breakouts.
- **Prompt:** hard-reject rubric (data-quality, earnings blackout, unexplained move, unconfirmed
  momentum), conviction→size table capped at headroom, synthetic-stop risk awareness, strict JSON.

### Deferred (needs backtest / more plumbing — flagged for owner judgment)
- **`prev_close`-based momentum** (gap-and-go): screen on `max(move-from-open, move-from-prev-close)`;
  needs prev_close threaded through the fetchers + `SIGNAL_THRESHOLD` re-tune.
- **Volatility/ATR-scaled sizing** layered under the notional cap (high-IV names sized down).
- **`dd_probe` history fallback** (Yahoo-only today → blind on a Yahoo miss, e.g. GOOGL); add a
  second daily-history source so trend/MA/volume flags aren't false-on-missing-data.
- **Deterministic earnings backstop** (fetch the date in `dd_probe`, enforce the blackout in code,
  re-check at execution) instead of trusting the LLM-self-reported date.
- **Trailing stop** (persist a high-water mark), **slippage model** for honest paper fills, a
  **US-holiday calendar** in `session_state`, and **cache-keying on price/regime** drift.
