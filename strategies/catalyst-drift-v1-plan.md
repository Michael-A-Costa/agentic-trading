# Catalyst-Drift Engine — v1 Plan (supersedes momentum-v0 for live deployment)

> **⚠️ SUPERSEDED same day (2026-06-05): pivoted to FREE-REIN.** The owner found catalyst gap-drift too
> passive — "only buy a ticker that gapped >7% overnight" left the book in cash all session. This is a
> fun, risk-it-for-the-biscuit sleeve, so the mechanical entry gate was removed entirely and the agent
> given full discretion over WHAT to buy (any liquid mover) and HOW LONG to hold (scalp/swing/runner),
> with stops + caps + the daily breaker as the only seatbelts. The gap-drift research/backtests below
> stay valid as ANALYSIS (the only *measurable* edge), but the live engine no longer trades them — the
> agent's own read is the strategy. The forward ledger (`catalyst_log.py` / `catalyst_filter_report.py`)
> now measures general agent selection skill: does the agent's commit beat the average candidate it
> evaluated, forward. No proven edge here — it's discretion + seatbelts, on purpose.

**Owner mandate (2026-06-05):** this is the **risky** sleeve — the owner already holds the safe,
long-term, large-cap book elsewhere. This account exists to take **high-variance, actively-managed,
agent-driven** bets. Decisions locked via Q&A:
- **Horizon:** multi-day, actively managed. Hold winners days-to-weeks where the edge is; the agent
  monitors all day, trims/adds/cuts. Risk from concentration + beta, **not** intraday churn.
- **Universe:** unconstrained — agent ranges across small/mid/large by catalyst conviction, within
  risk caps.
- **Hedging:** directional only, no hedge. Risk expressed via sizing + stops. (Forced anyway: account
  `your_account_number` is **cash** + RH MCP is **equities-only beta** — no shorting, no options. See
  `memory/cash-account-gfv-constraint.md`.)

**Evidence basis (`research/signal-backtests.md`):** the intraday absolute-pop signal is
anti-predictive (reversal); cross-sectional single-name momentum was **survivorship bias** (collapses
to t=0.30 dropping the 10 best names, |t|<0.6 on never-delisting sector ETFs). The **only** surviving
edge is **catalyst gap-drift / PEAD**: an overnight gap on a volume spike drifts forward over 10–20
days (LARGE-cap control **t up to 3.10**). This plan builds the engine around that one edge.

---

## What changes vs the momentum/pop engine

| Dimension | Pop engine (current, live) | Catalyst-drift engine (v1) |
|---|---|---|
| **Entry trigger** | intraday move vs **open** ≥ `SIGNAL_THRESHOLD_PCT` + rel-strength vs SPY | overnight **gap** vs **prev close** ≥ `GAP_THRESHOLD_PCT` **AND** volume ≥ `VOL_MULT_MIN`× 20d-avg (a catalyst proxy) |
| **Alpha filter** | LLM momentum-DD commit/reject | LLM **catalyst classifier**: name the catalyst, confirm it's real/durable (earnings beat, guidance, contract, M&A, FDA…), **reject pumps / low-float squeezes / sympathy** — the load-bearing piece for small/mid |
| **Horizon** | EOD-flatten, 120-min max-hold | **multi-day**, `MAX_HOLD_DAYS` ≈ 10–20; **resting broker stops mandatory** (overnight gap risk) |
| **Universe** | top gainers, $2B+ mcap floor | unconstrained gainers/gappers, **mcap floor lowered**, **$-volume + price floors kept** (must stay exitable) |
| **Sizing** | 5% per name, 10 names, 4% stop | **concentrated** (fewer, larger), **wider stop** (~8%, high-beta multi-day), **ATR/vol-scaled** so each bet risks ~equal $ |
| **Cadence** | ~5-min cron | catalyst scan a few×/day + position monitoring; stops rest at the broker (no 5-min need) |

---

## File-mapped changes

1. **`scripts/discover.py`** — switch the screen from "today's % gainers" to "**overnight gappers on a
   volume spike**": compute `gap = open/prev_close − 1` and `vol_mult = vol/avg20`. Lower
   `MIN_MARKET_CAP_USD` (e.g. $2B → ~$300–500M) to admit small/mid, but **keep `MIN_DOLLAR_VOL` and
   `MIN_PRICE`** so a multi-day position is exitable. (This is the deferred `prev_close`-based
   gap-and-go item, now the primary entry.)
2. **`scripts/tick_context.py`** — thread `prev_close` + `avg20_vol` through the fetchers; replace the
   `SIGNAL_THRESHOLD_PCT`/`REL_STRENGTH_PCT` gate with the gap+volume gate; add a **`MAX_HOLD_DAYS`**
   day-count exit (replaces `MAX_HOLD_MIN`); turn **off** `FLATTEN_BEFORE_CLOSE_MIN` /
   `WINDDOWN_*` / `NO_ENTRY_LAST_MIN` (we now hold overnight).
3. **`scripts/decide.py` + `scripts/dd_prompt.txt`** — rewrite the prompt from momentum-DD to
   **catalyst taxonomy + durability + pump detection**. Output: catalyst type, is-it-real, durability
   (days the drift should persist), conviction → size. Reject names where the move has no identifiable
   durable catalyst. This is where the agent earns its keep on the dirty small/mid signal.
4. **`scripts/apply_decision.py` / `scripts/live_execute.py`** — exits become **multi-day**: persist
   `entry_date`, count holding days, exit at `MAX_HOLD_DAYS` or stop. **Resting `stop_market` GTC is
   now mandatory**, so **prefer whole shares** (`PREFER_WHOLE_SHARES=1`) — fractional's synthetic stop
   does **not** cover overnight gaps. Add ATR/vol-scaled sizing under the notional cap. Consider a
   trailing stop (high-water mark) to let drift run.
5. **`.env`** — new knobs: `GAP_THRESHOLD_PCT`, `VOL_MULT_MIN`, `MAX_HOLD_DAYS`, `ATR_SIZING`,
   `TRAIL_STOP_PCT`; retune `STOP_LOSS_PCT` (4→~8), `MAX_POSITION_PCT`↑ / `MAX_OPEN_POSITIONS`↓ for
   concentration (owner's call), `FLATTEN_BEFORE_CLOSE_MIN=0`, `MIN_MARKET_CAP_USD`↓.
6. **`scripts/run_trading_tick.sh` + `com.agentic.trading-tick.plist`** — drop the ~5-min cadence to a
   few scans/day; the token economics (`MAX_DD_CANDIDATES`, Sonnet DD) re-scale down.

---

## Risk model for a risky directional book (no hedge)
The seatbelt is unchanged but retuned for a hotter book: per-name + total-exposure caps,
`MAX_PER_TRADE_LOSS_PCT`, the daily circuit breaker, and **mandatory resting stops** are the only
downside control (no hedge, no short). Going unconstrained into small/mid means **liquidity is the
hard floor** — never take a position a resting stop can't exit. ATR-scaled sizing keeps a 3% gap and a
30% gap from risking wildly different dollars. The GFV/T+1 risk *eases* vs the intraday-recycle pattern
(multi-day holds settle), but rotation days still need the settled-cash guard (P3 in the landscape memo).

## Validation gates before re-arming live
1. **Portfolio backtest** — DONE (`scripts/backtest_catalyst_book.py`, see `signal-backtests.md`
   Backtest 4 / Conclusion 5). Result: per-trade expectancy is **positive** (+1.4–1.95%/trade,
   unfiltered, net of costs), but the 100-name book is **capital-starved** (~80–90% cash) and, forced
   to deploy, lands **≈ SPY risk-adjusted**. The edge case rests on three **unmodeled** levers that are
   the agent's actual job: (a) market-wide breadth (stay deployed on *high-quality* gap≥10% signals,
   not 100 fixed names), (b) the catalyst/pump-filter (lift the 42–49% win rate), (c) MIDCAP de-biasing.
   **Design locks from the sim:** exits are **time-based (~15–20d) + hard stop — do NOT use a tight
   trailing stop** (it cut the drift and collapsed Sharpe to 0.05); favor **high gap thresholds + breadth**
   over low thresholds + dilution.
2. **Paper-run** the rebuilt engine and **measure whether the agent's catalyst-confirmation lifts win
   rate above the ~45% unfiltered baseline** — that lift is the whole thesis; if it doesn't appear in
   paper, the book is a SPY-tracker and not worth live risk.
3. Only then **re-arm live** (canary first), sized small. Until then: the live engine is running the
   *dead* pop strategy — **pause it** (flip to paper / disarm) rather than keep real money in
   negative-EV trades.

## Build order
1. ✅ `scripts/backtest_catalyst_book.py` — portfolio sim (gate #1).
2. ✅ Entry-signal swap (gap+vol) — `market_conditions.py` (`daily_bars_cached`/`catalyst_signal`
   helpers), `tick_context.py` (gate now gaps + volume, ranks by gap; multi-day `MAX_HOLD_DAYS` exit;
   EOD-flatten/winddown/max-hold-MIN off), `decide.py` (DD packet `screen_signal` = gap/vol).
   `discover.py` universe widened via `.env` `MIN_MARKET_CAP_USD` $2B→$300M (no code change — it reads env).
3. ✅ `dd_prompt.txt` — catalyst-classifier + pump-filter rewrite; earnings logic flipped (a just-passed
   beat is a valid trigger; reject only *holding into* the next print within `MAX_HOLD_DAYS`).
4. ✅ `.env` retune — gap/vol knobs, MULTI-DAY horizon, wider 8% stop, concentration (15%/name, 6 names,
   90% exposure), `MAX_PER_TRADE_LOSS_PCT` 0.01→0.02 (so the per-trade-loss cap doesn't throttle the
   wider stop), scale-out OFF, far 25% TP. Still `TRADING_MODE=paper`, `LIVE_ARMED=0`.
5. 🔄 IN PROGRESS: paper-validating (engine live via launchd, 5-min, paper). **Forward filter-lift
   ledger built** — `catalyst_log.py` logs every agent-evaluated gap event {verdict, gap, vol,
   ref_price, is_real/is_pump} to `data/catalyst_events.jsonl`; `catalyst_filter_report.py` joins each
   to its realized N-day forward drift (keyless daily history) and reports REAL vs PUMP vs the gap-alone
   baseline. Leakage-free (forward, no hindsight) — the historical version is contaminated by the LLM
   already knowing the outcome. Read the report (`python3 scripts/catalyst_filter_report.py`) as events
   accumulate; the lift of REAL over gap-alone is the whole thesis.
6. ⏳ TODO (live-only, before re-arming): in `live_execute.py` make the resting `stop_market` GTC
   mandatory for overnight whole-share lots + verify it persists across an overnight boundary on the
   canary; ATR/vol-scaled sizing under the notional cap; cadence drop (5-min → a few scans/day) in
   `run_trading_tick.sh` + the plist.
