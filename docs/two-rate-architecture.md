# Two-rate control loop — fast sentinel + slow planner

**Status:** v1 (2026-06-05). Paper mode.

## Why

A deep-dive (DD) is a ~85s Sonnet+web call — the dominant cost and latency of a tick. We were
running the *whole* loop at one cadence, which forces a bad trade-off:

- **Fast cadence (1-min):** good risk latency, but a DD-heavy tick can't even finish inside the
  interval (ticks overlap / get starved), and token spend balloons.
- **Slow cadence (5-min):** cheap, but protective exits on synthetic-stop (fractional) positions
  lag up to 5 minutes.

The fix is to split *monitoring* (cheap, must be fast) from *deliberation* (expensive, can be
slow) — the classic two-rate controller. A slow **planner** sets intent; a fast **sentinel**
enforces it at high frequency with no LLM in the hot path.

## The two loops

| | **Planner** | **Sentinel** |
|---|---|---|
| Cadence | 5 min (`StartInterval 300`) | 1 min (`StartInterval 60`) |
| Entry | `run_trading_tick.sh` → `decide.py` → `apply_decision.py` | `sentinel.py` |
| LLM | yes — discovery, DD, manage re-assessment | **never** |
| Responsibility | find names, DD them, commit/arm entries, set stops/TPs | fire all protective exits + crossed armed entries |
| Cost | tokens | quotes + arithmetic (~free) |

The two loops do **not** block each other. Instead of a coarse shared lock, every `paper_state.json`
read-modify-write runs under a short-held `data/.state.lock` (held for milliseconds, never during
the planner's DD or the sentinel's quote fetch). So the sentinel fires exits every minute even while
a planner tick is mid-DD — the slow loop never starves the fast one. The planner's own
`data/.tick.lock` remains, but only as planner single-flight (two planner ticks can't overlap).

## Shared context

`tick_context.build_context()` is the single source of truth for a tick's view: fresh quotes,
equity-resolved caps, per-position P&L, and the deterministic `screen.exits` (stops, take-profits,
Tier-1 risk soft-cut, scale-out tiers, time-exits). Both loops call it, so the sentinel evaluates
*exactly* the same exit rules the planner does — just 5× more often. `apply_decision.validate_and_fill`
is the shared execution primitive (cap re-checks + paper fill + state mutation); the sentinel reuses
it verbatim, so a sentinel fill and a planner fill are identical.

## Armed entries ("LLM arms, fast loop fires")

The strong form of "only call the LLM on a threshold cross": the planner does the thinking *up
front* and leaves a deterministic trigger the sentinel can fire — keeping the hot path token-free
*and* low-latency.

- A planner commit may include `entry_trigger: {price, direction}` (`direction` = `above` for a
  breakout, `below` for a pullback/limit).
- `apply_decision` sees `arm: true` on a buy action and **stashes** it into
  `state.armed_entries[SYM]` instead of filling — `{trigger_price, direction, dollar_amount,
  conviction, hold_intent, thesis_type, reason, armed_ts, expires_ts}`.
- The sentinel, each minute, checks every armed entry against the fresh quote. On a cross (and if
  `allow_entries` is true and it hasn't expired) it fires the buy through `validate_and_fill`
  (full cap gate) and consumes the trigger. Expired triggers are dropped.
- **Default is unchanged behavior:** a commit with *no* `entry_trigger` fills immediately, exactly
  as before. The DD prompt (`dd_prompt.txt`) now offers `entry_trigger` for breakout-confirmation
  (`above`) and pullback (`below`) entries, with guidance to reserve it for when the level genuinely
  is the thesis — so arming is live in production but the default stays immediate. `ENTRY_ARMING=0`
  disables it engine-wide.

Triggers are re-validated/re-armed by the planner each 5-min tick; an armed entry's natural TTL is
`ENTRY_ARM_TTL_MIN` (default one planner cycle's worth of slack).

## Locking & ownership

- Every `paper_state.json` writer — `apply_decision` (planner fills + arming), `sentinel` (fast
  fills), `tick_context` (start-of-day equity rollover) — wraps its read-modify-write in
  `state_lock.state_lock()` (`data/.state.lock`), an atomic mkdir lock held only for the mutation.
  `apply_decision` does no LLM/network work, so its critical section is milliseconds; the sentinel
  does its quote fetch *before* taking the lock. Writers re-read state fresh under the lock
  (optimistic concurrency), so a stale planner decision can never clobber a sentinel exit — at worst
  an action no-ops ("no position to sell"). The lock reclaims a holder older than 15s and fails open
  after 20s (better a rare interleaved write than a missed stop).
- `data/.tick.lock` stays, but now only as planner single-flight (run_trading_tick.sh) — the
  sentinel no longer touches it.
- The planner path is the only one that calls the LLM; the sentinel has no MCP/model access.

## Files

- `scripts/tick_context.py` — `build_context()` (shared) + `main()` (planner: writes packet, prints GATE).
- `scripts/sentinel.py` — the fast loop.
- `scripts/apply_decision.py` — arming support in the executor.
- `scripts/com.agentic.sentinel.plist` — 1-min launchd job.
- `scripts/com.agentic.trading-tick.plist` — planner, bumped to 300s.

## Future

- Decouple manage-DD cadence from entry-DD cadence now that exits no longer need the planner.
- Watch armed-entry usage in the logs: if the agent over-arms (triggers expiring unfilled → re-DD
  churn), tune `ENTRY_ARM_TTL_MIN` up or tighten the prompt guidance.
