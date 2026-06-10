# Agentic Trading Engine — Methodology

**As of:** 2026-06-09  
**Account:** Robinhood cash account (equities-only; no shorting, no options, no margin)  
**Status:** Live, small-sized, with cumulative tripwire

---

## 1. What This Is

An autonomous trading engine that runs on a dedicated Robinhood account. An LLM agent (Claude Sonnet) evaluates the day's market movers, decides which to buy, and manages the resulting positions — all without per-trade human approval. Risk controls enforced in code (position caps, stop-losses, a daily circuit breaker) are the primary safety layer.

This is an explicitly **high-variance, actively-managed** sleeve. The account owner carries a separate long-term, diversified portfolio elsewhere; this account exists to take concentrated, agent-driven bets.

---

## 2. Research Basis

Three signal hypotheses were backtested before building this engine:

| Signal | Backtest verdict |
|---|---|
| Intraday absolute pop (vs. open) | Anti-predictive at the 1-day horizon — reversal signal, not momentum. Discarded. |
| Cross-sectional momentum (top gainers) | Edge collapses to t=0.30 after dropping the 10 best winners; |t|<0.6 on never-delisting ETFs. Was survivorship bias. Discarded. |
| Catalyst gap-drift / PEAD | Overnight earnings gap on volume spike drifts forward over 10–20 days. LARGE-cap control: t up to 3.10. **The only surviving edge.** |

The engine is built around PEAD. The validated edge is: an overnight gap (vs. prior close) ≥ ~7% on ≥ 2× average daily volume, on a large-cap name, produces measurable forward drift over 10–20 trading days. Per-trade expectancy is positive (+1.4–1.95%, net of costs), but the edge is noisy (win rate ~45–49% unfiltered).

---

## 3. Candidate Discovery

Two sources are merged each tick:

**PEAD calendar** (`discover_pead.py`): pulls the Nasdaq earnings calendar for the last 4 calendar days. Any stock that reported earnings and passes basic eligibility (market cap ≥ $300M, not an ETF/fund, not on the exclusion list) is surfaced as a priority candidate regardless of gap direction. Gap direction is currently evaluated by the DD agent, not pre-filtered.

**Dynamic movers** (`discover.py`): today's top gainers from a keyless Nasdaq screener, filtered by:
- Price ≥ $8 (stops are meaningful above this)
- Market cap ≥ $300M
- Dollar volume ≥ $20M/day (position must be exitable)
- Not a recent IPO (optional; off by default)

Index ETFs and any symbols on the long-term exclusion list (`stock_memory.py`) are always removed before DD.

There is no fixed watchlist. The engine trades only what the market is moving that day.

---

## 4. Entry: Deep Due Diligence (DD)

Every candidate that survives discovery gets a Stage-2 DD: a ~85-second Claude Sonnet + web research call. The agent is given:

- The candidate's intraday move, range position, and days since earnings (if applicable)
- Current portfolio context (cash, exposure, held names)
- Market regime (index breadth, VIX proxy)

The agent decides: **commit**, **reject**, or **watch**. A commit includes a conviction level (high / medium / low) and an optional entry trigger price (for breakout or pullback confirmation). There is no mechanical signal gate — the agent's read is the strategy.

**DD economics:** Each DD is cached per ET calendar day. A cached reject is only re-evaluated on an upside price breakout (≥ 2% up, or range position pushing to a fresh intraday high). A cached commit is re-evaluated if price moves more than 5% in either direction. This keeps token cost bounded while ensuring materially changed setups get a fresh look.

**Async DD:** Cache misses are dispatched to detached background workers so they don't block the tick. Verdicts land on the next tick (typically 15 minutes later).

---

## 5. Sizing

Entries are sized by conviction, subject to hard caps resolved from live equity each tick:

| Conviction | Fraction of MAX_POSITION_USD |
|---|---|
| High | 1.0× |
| Medium | 0.6× |
| Low | 0.35× |

These tiers are **prompt-enforced, not mechanically enforced by code.** The DD agent is instructed to propose a `dollar_amount` matching its conviction tier; code then caps that value against the hard limits below. A LLM-sized breach of a hard cap is impossible, but the tier ratios themselves are advisory — a stochastic model, not a formula.

Hard caps (all fractions of live equity, resolved to dollars each tick):
- **Per-name ceiling:** 15% of equity (`MAX_POSITION_PCT=0.15`)
- **Total book exposure:** 90% of equity (`MAX_TOTAL_EXPOSURE_PCT=0.90`)
- **Per-trade max loss:** 2% of equity (`MAX_PER_TRADE_LOSS_PCT=0.02`; this bounds the notional × stop% product)
- **Max open positions:** 30

Orders are whole-share lots wherever possible. A whole-share entry gets a real resting stop-market GTC at the broker (survives overnight gaps). Fractional lots get only a synthetic stop, watched every minute by the sentinel process. Names priced above `MAX_POSITION_USD` are filtered before DD (not tradable at this account size).

All orders are marketable limits (0.5% above the quote touch), not naked market orders. An order that fails a pre-trade broker review (PDT, halt, buying power alert) is skipped and logged.

---

## 6. Exits

Four exit mechanisms. The trailing stop is a **stop-price ratchet** applied during reconcile — it does not participate in the tick-time evaluation order. Tick-time checks run in this order:

1. **Hard stop** (or trailing stop, once activated): sell if price ≤ current stop price. Whole-share lots: resting stop-market at the broker. Fractional lots: synthetic stop checked every 1 minute by the sentinel.

2. **Take-profit:** full exit at +40% (`TAKE_PROFIT_PCT`). Rarely binds by design — the trailing stop is intended to harvest drift rather than cutting winners at a fixed target.

3. **Soft-cut:** protective sell if a held position falls ≥ 8% intraday while below entry (`SOFT_CUT_PCT=8.0`).

4. **Time-exit:** any position held ≥ 21 calendar days (~15 trading days) is exited regardless of P&L. This bounds exposure to the PEAD drift window.

**Trailing stop ratchet:** separately, during reconcile, once a position is up ≥ 20% (`TRAIL_ACTIVATE_PCT`), the hard stop price is ratcheted up to trail 15% below the high-water mark (`TRAIL_STOP_PCT=15`). It only moves up, never down. Once active, it replaces the hard stop in check #1.

Additionally, a **Tier-1 protective sell** fires if a held position falls ≥ 8% intraday while below entry (`SOFT_CUT_PCT=8.0`). This is the backtested optimum; a 4% soft-cut was strictly worse than no soft-cut (it was cutting early into drift names and destroying ~0.4%/trade).

A **Tier-2 manage-DD** re-evaluates each holding's thesis on fresh news, at a frequency scaled by risk band (critical → every tick; high → ~15 min; medium → ~45 min; low → ~120 min).

---

## 7. Risk Controls

Controls are enforced in code. No trade is placed if it would breach a cap.

| Control | Parameter | Value |
|---|---|---|
| Daily circuit breaker | `DAILY_MAX_LOSS_PCT` | 5% of start-of-day equity |
| Daily loss cap | `DAILY_MAX_LOSS_CAP_USD` | $500 absolute (overrides the % above $10k equity) |
| Cumulative tripwire | `LIVE_TRIPWIRE_PCT` | 10% below live-start baseline; halts new entries, exits keep running |
| Per-trade loss | `MAX_PER_TRADE_LOSS_PCT` | 2% of equity per position |
| Position ceiling | `MAX_POSITION_PCT` | 15% of equity |
| Exposure ceiling | `MAX_TOTAL_EXPOSURE_PCT` | 90% of equity |
| Cash settlement guard | `CASH_SETTLEMENT_GUARD` | Sizes entries against settled cash only (no Good-Faith Violations on T+1 proceeds) |

Kill switches, in order of preference:
1. `launchctl unload com.agentic.trading-live.plist` — stops the scheduler
2. `LIVE_ARMED=0` in `.env` — dry-run mode (reviews print, nothing places)
3. Disconnect the Robinhood MCP — blocks all relay calls at the broker API level

---

## 8. Execution Architecture

The engine runs as two independent launchd jobs on a Mac:

**Planner** (every 15 minutes): discovery → DD → commit/reject → size → place orders + set stops. This is the deliberative loop: it calls the LLM, does web research, and writes state. A tick with a full DD pass takes ~2–3 minutes.

**Sentinel** (every 1 minute): reads live state + public quotes, checks synthetic stops and take-profits, fires protective sells if a level is breached. No LLM in the hot path. Protective exits have ~1-minute latency regardless of what the planner is doing.

The two loops share a lock file (`data/.tick.lock`) for single-flight planner execution. When the sentinel detects a breach it **does** acquire this lock before firing the sell — if the planner holds it, the sentinel defers and retries next minute. Because a planner tick with a full DD pass runs 2–3 minutes, worst-case protective exit latency is ~3–4 minutes (not ~1 minute as a clean sentinel pass would suggest). The lighter `data/.state.lock` is held by the sentinel for milliseconds when writing fill state.

All order placement goes through a pre-trade broker review (`review_equity_order`) before `place_equity_order`. Blocking alerts skip the trade; all decisions are logged to `data/engine-log.jsonl`.

---

## 9. What Is and Isn't Proven

| | Status |
|---|---|
| PEAD gap-drift edge (LARGE-cap, gap-up) | Backtested, t=3.10, 10–20d horizon |
| Agent catalyst-confirmation lift (does DD improve win rate vs. gap-alone?) | **Not yet proven.** Forward ledger accumulating since 2026-06-05; first cohort resolves ~2026-06-26. |
| Free-rein discretion (non-PEAD entries) | No backtested edge. Agent's own read. |
| Exit policy (8% soft-cut, no trailing stop until +20%) | Backtested on existing fills (2026-06-09). |

The engine was armed live (2026-06-08) before the evidence gate for agent lift was cleared. This was an owner decision, accepted with the 10% cumulative tripwire as the backstop. The forward ledger (`catalyst_filter_report.py`) measures whether agent commits outperform the average candidate evaluated; if after ≥30 resolved events the qualified-PEAD cohort shows no lift over the gap-alone baseline, the strategy will be disarmed.

---

## 10. Known Gaps

- **PEAD direction filter:** The PEAD screener surfaces earnings candidates regardless of gap direction. Negative-gap names (stock gapped down on earnings) are rejected by the DD agent but should be filtered earlier — a long-only account cannot trade them and they consume cache slots.
- **No overnight gap protection for fractional lots:** Synthetic stops are only checked every minute. A large overnight gap on a fractional position can breach the stop before the sentinel fires. Whole-share lots with resting broker stops are preferred for this reason.
- **Single-machine dependency:** The scheduler is a local launchd job. If the Mac is asleep when a scheduled tick should fire, launchd runs one coalesced tick on wake. Positions are protected by resting broker stops during any gap.
