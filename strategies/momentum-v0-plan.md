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
| `MAX_POSITION_PCT` | `0.10` | per-name ceiling = 10% of **live equity** (resolved to $ each tick); SINGLE concentration cap |
| `MAX_TOTAL_EXPOSURE_PCT` | `0.80` | ~80% of equity invested, keep a 20% cash buffer (fraction of live equity) |
| `MAX_OPEN_POSITIONS` | `10` | diversification (≥8 needed to reach 80% exposure at the 10% per-name cap) |
| `STOP_LOSS_PCT` / `TAKE_PROFIT_PCT` | `4.0` / `12.0` | per-position exits (~1:3 R:R) |
| `MAX_PER_TRADE_LOSS_PCT` | `0.01` | per-trade stop budget = 1% of live equity (=$30 at $3k); slack backstop at a 4% stop |
| `DAILY_MAX_LOSS_PCT` / `DAILY_MAX_LOSS_CAP_USD` | `0.05` / `500` | **circuit breaker** = min(5% of start-of-day equity, $500) — halt new entries (re-checked at fill; exits still run) |
| `SIGNAL_THRESHOLD_PCT` | `2.0` | absolute intraday entry trigger; ≥ 2× spread |
| `REL_STRENGTH_PCT` | `1.0` | also require this much intraday % **above SPY** (don't just buy beta) |
| `MIN_POSITION_USD` | `0` | reject dust fills (0 = off) |
| `COOLDOWN_MIN` | `30` | no re-entry into a name within N min of exiting (anti-whipsaw) |
| `FLATTEN_BEFORE_CLOSE_MIN` / `NO_ENTRY_LAST_MIN` | `15` / `15` | EOD flatten + block late entries |
| `WINDDOWN_BEFORE_CLOSE_MIN` / `WINDDOWN_MIN_PROFIT_PCT` | `0` / `1.0` | EOD wind-down: in the last N min, lock in **green** positions (`pnl% ≥ profit`) early rather than risk the gain into a choppy close. Asymmetric — losers keep full runway to the hard flatten. Set N > `FLATTEN_BEFORE_CLOSE_MIN`; `0` = off |
| `MAX_HOLD_MIN` / `STALL_BAND_PCT` | `0` / `2.0` | force-exit a STALLED position held > N min, but only when `|pnl%|` < band so a runner/bleeder keeps its price rule (`STALL_BAND_PCT<=0` = blind time stop; `MAX_HOLD_MIN=0` = off) |
| `SCALE_OUT_TIERS` / `SCALE_BREAKEVEN_AFTER_FIRST` | `""` / `1` | partial profit-take ladder `gain%:fracOfEntryQty,…` (e.g. `5:0.33,8:0.33`): trim a slice at each tier the gain clears, leaving the rest to ride to `TAKE_PROFIT_PCT`. After the first trim, ratchet the synthetic stop to breakeven. Empty = off (one all-or-nothing exit). Tiers ≥ TP never fire. |
| `DD_MODEL` / `MAX_DD_CANDIDATES` / `DD_CACHE_TTL_MIN` | Sonnet / `2` / `180` | Stage-2 commit model + cost bounds |

> **All caps above are enforced deterministically** in `apply_decision.py` (buy branch) and
> `tick_context.py` — the sizing caps (`MAX_POSITION_PCT`, `MAX_TOTAL_EXPOSURE_PCT`) are resolved to
> dollars against this tick's live equity, and `MAX_PER_TRADE_LOSS_USD` (bounds size so
> `notional × STOP_LOSS_PCT ≤` budget) is a real reject branch, not just config. `MAX_POSITION_PCT`
> doubles as the per-name concentration cap (it replaced the old `MAX_SYMBOL_WEIGHT`, which was the
> same `symbol_value / equity` formula once sizing became a fraction of equity).

### Order execution model: marketable limits + slippage + hybrid stops (paper-modelled now)
Fills are no longer free prints at the last quote. `apply_decision.py` models a real **marketable
order** on both sides (knobs in `.env`):
- **Entries** are a **marketable BUY limit**: limit capped `MARKETABLE_LIMIT_PCT` above the touch
  (price protection), filled at the touch **+ `SLIPPAGE_BPS`** (you pay up). A modeled fill past the
  limit is skipped — the same gate the live executor will reuse. The order is recorded with
  `order_type`, `limit_price`, `ref_price`, `slippage_bps` on the trail.
- **Exits** give up `SLIPPAGE_BPS` below the touch; `order_type` is `stop_market` for stop hits,
  `market` for EOD-flatten / wind-down / max-hold, else `marketable_limit`. Risk-rule exits carry no
  limit cap (they must complete).
- Sizing is off the **fill price**, so slippage costs shares, not hidden P&L — paper P&L is now a
  conservative floor, not an optimistic one.

### Stop protection: hybrid tag now, resting-broker enforcement live
Every buy attaches an explicit `stop_price` (−`STOP_LOSS_PCT`) and `take_profit_price`
(+`TAKE_PROFIT_PCT`). The lot is tagged **`stop_type: "resting"`** when it's a whole-share lot
(`PREFER_WHOLE_SHARES=1` floors affordable buys to whole shares) and **`"synthetic"`** when
fractional. **Synthetic = our engine sells when it checks the level at tick time (~5 min) and the
host is awake** — *not* a resting broker order, so it does **not** cover between-tick moves,
overnight/pre-market gaps, or a crashed/asleep engine.

- **Paper / now:** both tags still **sell at the next tick** — resting vs synthetic fill identically
  in the sim. The tag exists for record fidelity and to drive the live executor; the real protection
  edge of a resting order only materializes live. Agent is always free to sell; no open-order lock.
- **Live (BUILT — `scripts/live_execute.py`):** Robinhood resting stops need **whole shares**, and
  fractional is market-only — so a real resting stop only rides on whole-share-eligible lots. The
  hybrid: whole-share lot → marketable **`limit`** entry + real resting **`stop_market`** GTC (not
  stop-limit — a limit can gap through and never fill); fractional → **`market`** entry + synthetic
  stop. Fractional sizing stays the default; whole-share is never forced. Each tick the executor
  reads the broker snapshot, **arms** the resting stop off the confirmed cost basis (one tick after
  the entry fills; the synthetic stop covers the gap), **cancel-stop → sell** for discretionary
  exits, and **reconciles** the "stop fired while asleep" case (position gone from broker → book it,
  clear metadata, start cooldown).

### Live execution architecture (how orders actually reach the broker)
Python can't call the Robinhood MCP — only a Claude agent can. The live path reuses
`decide.py:run_claude`: **all** sizing/cap/gating logic is Python (`live_execute.py`), which then
spawns a **tightly-scoped relay agent** (`rh_mcp.py`, minimum RH tools, no web/fs) to execute a
precise recipe and echo strict JSON. **Truth is always re-read from the broker** — the agent's prose
is never trusted; fills/closures are reconciled from `get_equity_positions` / `get_equity_orders`.
- **Tick flow (live):** `broker_snapshot.py` (real buying power/positions/orders, fail-closed) →
  `tick_context.py` (screen off broker truth + our metadata) → `decide.py` → `live_execute.py`.
- **State:** broker is source of truth for cash/qty/cost; `data/live_state.json` holds only our
  metadata (stop/TP/entry_ts/scaled/`resting_stop_order_id`, SOD equity).
- **Two-step arming:** `TRADING_MODE=live` + `LIVE_ARMED!=1` = **dry-run** (runs `review_equity_order`
  for real alerts, logs intended orders, places nothing). `LIVE_ARMED=1` places for real; the first
  order is capped to `LIVE_CANARY_USD` until one round-trip completes.
- **Gate (no human per-trade):** account hard-pin · `review` before every `place` with blocking-alert
  skip · `ref_id` idempotency · caps re-checked vs fresh buying power · daily breaker on broker SOD ·
  fail-closed snapshot. **Kill switch:** set `TRADING_MODE=paper`, set `LIVE_ARMED=0`, or disconnect
  the MCP.

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
- **Guardrails now real:** the per-name concentration cap (`MAX_POSITION_PCT`, formerly
  `MAX_SYMBOL_WEIGHT`) + `MAX_PER_TRADE_LOSS_USD` enforced at fill (were
  dead config); daily circuit breaker re-checked at fill (not just the gate); NaN/inf sizes and
  dust (`MIN_POSITION_USD`) rejected; exposure valued at `max(last, entry)` (never under-counts).
- **Risk inversion fixed:** on a `circuit_breaker` SKIP the engine still runs **protective exits**
  (it halts entries, not stops) — but stays idle on stale/closed-market SKIPs.
- **Atomic state writes** + refuse-on-corrupt-state (no silent re-baseline of the breaker).
- **Live-mode fail-closed:** the wrapper and paper executor refused `TRADING_MODE=live` until a real
  `review → place` path existed. *(Superseded: the live path is now BUILT — `live_execute.py` +
  `rh_mcp.py`; see "Live execution architecture" above. Live still fails closed on a missing broker
  snapshot, and only places when `LIVE_ARMED=1`.)*
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
