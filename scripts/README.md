# scripts/

Headless jobs for the agentic-trading engine. Two groups:

1. **Market-data / regime** (`market_conditions.py`, `run_market_check.sh`) — read-only, public
   data, touch no account. Stdlib-only.
2. **Trading tick pipeline** (`run_trading_tick.sh` → `tick_context.py` → `dd_probe.py` →
   `decide.py` → executor) — the actual engine. In **PAPER mode** (`apply_decision.py`) it simulates
   fills against live public quotes and tracks a local portfolio in `data/paper_state.json`; it
   places **no real orders**. In **LIVE mode** (`broker_snapshot.py` + `live_execute.py`) it places
   real MCP orders (`review → place`) — gated by `LIVE_ARMED` (dry-run vs armed). See **Trading
   engine** and **Live trading** below. Default ships at `TRADING_MODE=paper`.

**Prereqs:** the data/regime scripts are stdlib-only. The tick pipeline additionally needs the
`claude` CLI on `PATH` (or `AGENTIC_CLAUDE`) for the Stage-2 DD call, and Python 3.11 (or
`AGENTIC_PYTHON`). Config is sourced from `.env` (see `.env.example`).

## `market_conditions.py` — market-regime checker
Pulls index ETFs (SPY/QQQ/IWM/DIA) + VIXY (VIX proxy), classifies the session's
**posture / volatility / breadth**, prints a one-line summary, and appends a structured record
to `data/market_conditions.jsonl`. **Stdlib only** — nothing to `pip install`. Self-accumulates
SPY closes so a trend signal comes online after ~5 logged sessions.

**Data sources — automatic failover (logged as `source`):** every run tries them in order until
one returns index data, so a single provider outage/throttle doesn't blind the engine. All three
are keyless and independent:
1. **Stooq** (primary) — one batch CSV request for all symbols, + one retry.
2. **Cboe** delayed-quotes JSON (fallback 1) — per-symbol.
3. **Yahoo** chart API (fallback 2) — per-symbol.

If all three fail, the run logs an `error` record (and exits non-zero) rather than crashing.

```bash
python3 scripts/market_conditions.py          # check + log + summary
python3 scripts/market_conditions.py --json   # full record as JSON
python3 scripts/market_conditions.py --quiet  # log only
```

Why public data, not the Robinhood MCP: the MCP authenticates through the interactive Claude
client and **may be absent in a headless/cron run**. Market-regime data is public, so this job
is fully self-contained and runs anywhere.

## `run_market_check.sh` — cron/launchd wrapper
Cron-safe wrapper: absolute paths, minimal-PATH tolerant, never fails the scheduler. Adds a
dated run log under `data/logs/` on top of the JSONL record.

```bash
scripts/run_market_check.sh
```

Override the interpreter if needed: `AGENTIC_PYTHON=/path/to/python3 scripts/run_market_check.sh`.

---

## Trading engine (one tick) — `run_trading_tick.sh`

One PAPER tick, driven by the launchd agent every 5 min (`com.agentic.trading-tick.plist`). The
LLM is invoked for **one thing only** — the Stage-2 DD commit/reject call — so a tick costs just
the DD tokens. Everything else (gather, screen, exits, sizing checks, fills, logging) is
deterministic Python:

1. **`market_conditions.py`** — refresh the market regime (posture / volatility / breadth).
2. **`tick_context.py`** — gather quotes + portfolio, compute P&L, and run the **deterministic
   Stage-1 screen**: protective exits (stop / take-profit / EOD-flatten / max-hold) and entry
   candidates (intraday movers that also clear a relative-strength-vs-SPY bar, not held, not in
   post-exit cooldown). Decides the **GATE** (`TRADE` / `SKIP:<reason>`), including the daily-loss
   circuit breaker. Writes `data/tick/context_latest.json`.
3. **`dd_probe.py`** (Stage-2, per candidate) — deterministic quant DD. No LLM. All **keyless**.
   - Intraday (Cboe live quote): move %, gap, range position, $-volume, spread, IV → flags
     `spread_ok`/`liquid`/`iv_ok`/`parabolic`.
   - Daily history (Cboe's CDN `…/charts/historical/{SYM}.json` — keyless, deep, reliable; Yahoo is
     a 429-prone fallback): MA20/50, multi-day returns, 3-mo high/low, and **pace-adjusted**
     rel-volume (today's partial volume scaled by session-elapsed) → flags `trend_up`/
     `volume_confirmed`/`extended`/`at_high`.
   - Daily bars are **cached per symbol** in `data/history/{SYM}.json`, refreshed once per ET day
     (bars only finalize after the close), so history is fetched once/symbol/day instead of every
     tick — and if Cboe is briefly unreachable, the last-known-good file keeps DD running.
   - If every history source fails *and* there's no cache, `history_ok=false` and those last flags
     are `null` (unknown, never `false`) so a data gap can't masquerade as weak momentum; the model
     then judges on intraday + catalyst.
4. **`decide.py`** — for each candidate: run `dd_probe`, then the Stage-2 DD model (`DD_MODEL`,
   default Sonnet, with WebSearch/WebFetch + a live MCP quote) returns commit/reject + size.
   Per-symbol cache with split TTLs — commits reused for `DD_CACHE_TTL_MIN`, rejects for the shorter
   `DD_REJECT_TTL_MIN` (so discovery re-surfacing the same movers doesn't re-burn DD every tick);
   errors are never cached. Each DD is **primed with our long-term memory** of the name and writes
   its main points back (see below).
   - **`stock_memory.py`** — long-term, per-symbol evaluation memory (`data/stock_memory.json`),
     separate from the short DD cache. Every verdict's main points (decision, summary, catalysts,
     risks, next earnings) are saved and fed into the *next* DD as `prior_evaluation`. DD can flag a
     name `never_buy` (structural disqualifier — fraud, going-concern, serial diluter, delisting),
     which permanently **excludes** it: excluded names are filtered out of discovery and the screen,
     never quoted or researched again. Manage manually:
     `python3 scripts/stock_memory.py [show SYM | exclude SYM "why" | allow SYM]`.
5. **Executor + gate** — re-validates every action against the `.env` caps (the LLM is advisory;
   this is the real guardrail) and appends the full what+why record to `data/engine-log.jsonl`:
   - **paper** → **`apply_decision.py`**: models a marketable-limit fill with slippage, attaches the
     synthetic/resting stop tag + take-profit, updates `data/paper_state.json`.
   - **live** → **`live_execute.py`**: turns each action into real MCP orders (marketable-`limit`
     entry + resting `stop_market` GTC for whole-share lots; `market` + synthetic for fractional),
     re-checking caps against fresh broker buying power. See **Live trading** below.

On a `circuit_breaker` SKIP the engine still runs **protective exits** (it halts new entries, not
stops). On `market_closed` / stale-data SKIPs it stays idle (live still reconciles broker stops/closures).

```bash
scripts/run_trading_tick.sh                 # one paper tick by hand
ALLOW_OFFHOURS=1 scripts/run_trading_tick.sh # force a tick when the market is closed (testing)
tail -n 30 data/logs/tick_$(date +%F).log   # human trail: per-candidate WHY (commit/reject + reason)
tail -n 5  data/engine-log.jsonl            # full machine audit trail (dd reason/catalysts/risks)
```

### Trade history (what was actually done)

Separate from the fat per-tick `engine-log.jsonl`, **every executed fill** (paper fill *or* live
placed order) is mirrored to a dedicated, mode-tagged trade history via **`trade_log.py`** (shared by
both executors, so paper/live never drift):

- **`data/trades.jsonl`** — one compact JSON row per trade (greppable: `grep NVDA data/trades.jsonl`).
- **`data/journal/trades-<ET-date>.md`** — human-readable daily blotter, one bullet per trade.

```bash
python3 scripts/trade_ledger.py --mode live # blotter + round-trips, LIVE truth only (FIFO entry→exit)
python3 scripts/trade_ledger.py --symbol NVDA   # one name's whole life
python3 scripts/trade_ledger.py --round-trips --since 2026-06-04  # closed trips: hold time, P&L, P&L%
python3 scripts/pnl_report.py --mode paper  # realized P&L + exit-type breakdown (off the engine log)
python3 scripts/exit_counterfactual.py      # let-run replay on ACTUAL fills vs what the policy did
cat data/journal/trades-$(date +%F).md      # today's blotter at a glance
```

`trade_ledger.py` reconstructs round-trips (hold time, entry/exit price, P&L%) the per-tick log
can't show; `pnl_report.py` stays the realized-P&L/exit-type summary. Both share one exit-type
classifier (`trade_log.classify_exit`). **Both take `--mode paper|live|live-dryrun|all`** and
default to `$TRADING_MODE` (else `all`, labeled MIXED) — paper and live stats blended silently is
how the live win-rate question went unanswerable (remediation plan P4). Live order rows track a
lifecycle (`placed` → `filled` / `dead`, deduped by `order_id` in the readers): a `placed` row is
an intent, only `filled` is a real execution, and a `dead` row means the entry never filled
(blotter shows `[NOT FILLED]`). Positions closed at the broker while the engine slept (resting
stop fired) are booked by reconcile as `closed_external` sell rows (P6).

Each tick's console/`tick_*.log` now spells out the **why**, not just counts: every screened
candidate's signal, its Stage-2 DD verdict (`COMMIT`/`REJECT`/`ERROR`) with the model's reason and
whether it was a `[fresh Ns]` call or a `[cached Nm]` reuse, any cap rejections, and the final
HOLD/fill line — so a no-trade tick says *why* nobody traded. `tick end (Ns)` shows tick latency
(cached ≈1s, a fresh DD ≈50s).

---

## Scheduling it headless (this machine's TZ is US/Eastern, so cron times = ET)

### Option A — cron (simplest)
```bash
crontab -e
```
Add (every 30 min, weekdays, ~market hours; the script self-labels off-hours runs, so the
edge ticks at :00/:30 around the open/close are harmless):
```cron
*/30 9-16 * * 1-5 /ABSOLUTE/PATH/TO/agentic-trading/scripts/run_market_check.sh >/dev/null 2>&1
```
**macOS gotchas:**
- Grant **Full Disk Access** to `/usr/sbin/cron` (System Settings → Privacy & Security → Full
  Disk Access) or cron can't read files under `~/Documents`.
- The Mac must be **awake** during runs. Keep it awake on a schedule with `caffeinate`, or via
  `pmset`. Sleep = missed ticks.

### Option B — launchd (more robust on macOS)
Create `~/Library/LaunchAgents/com.agentic.marketcheck.plist` running `run_market_check.sh` on a
`StartCalendarInterval`, then `launchctl load` it. launchd survives reboots and is the native
scheduler; ask and I'll generate the plist.

### Verify it's running
```bash
tail -f data/logs/market_check_$(date +%F).log     # run log
tail -n 5 data/market_conditions.jsonl             # structured records
```

## Headless MCP — VERIFIED working (2026-06-04)
The trading engine needs the Robinhood MCP (account data + orders), and **headless access is
confirmed**. A non-interactive probe reused the stored OAuth token with no browser step:
```bash
claude -p 'Call get_accounts and report only the count' \
  --mcp-config /ABSOLUTE/PATH/TO/agentic-trading/.mcp.json \
  --allowedTools 'mcp__robinhood-trading__get_accounts' \
  --dangerously-skip-permissions --output-format text
# -> {"ok": true, "num_accounts": 4}  (exit 0, ~15s, no interactive auth)
```
So the autonomous engine path is: **cron → `claude -p` (headless) in this repo → MCP tools**,
scoped by `--allowedTools` and the `.env` risk caps. The market-conditions cron above stays
independent (public data, no auth) as a robust regime feed the engine can read.

---

## Live trading (`TRADING_MODE=live`)

Python can't call the Robinhood MCP — only a Claude agent can — so live orders go through a
tightly-scoped relay agent (`rh_mcp.py`), while **all** sizing/cap/gating logic stays in Python
(`live_execute.py`). **Truth is always re-read from the broker**; fills and closures are reconciled
from `get_equity_positions` / `get_equity_orders`, never from the agent's prose.

**Tick flow (live):** `broker_snapshot.py` (real buying power/positions/orders → `data/tick/
broker_snapshot.json`, **fail-closed**) → `tick_context.py` (screens off broker truth +
`data/live_state.json` metadata) → `decide.py` → `live_execute.py`.

**Order semantics** (pinned by the MCP schema — `dollar_amount`/fractional are market-only; `limit`/
`stop_market` need whole shares): whole-share lot → marketable **`limit`** entry + resting
**`stop_market`** GTC (armed off the confirmed cost basis one tick after the fill; synthetic stop
covers the gap); fractional → **`market`** entry + synthetic engine-tick stop.

**Two-step arming (the seatbelt):**
```bash
# 1) DRY-RUN: real review_equity_order alerts, logs intended orders, places NOTHING
TRADING_MODE=live LIVE_ARMED=0 scripts/run_trading_tick.sh
tail -n 3 data/engine-log.jsonl     # look for "buy_dryrun" / "DRY-RUN would place"

# 2) ARMED: real orders, each sized to MAX_POSITION_USD within the exposure/settled-cash caps
TRADING_MODE=live LIVE_ARMED=1 scripts/run_trading_tick.sh
```
**Gate (no human per-trade approval):** account hard-pinned to `AGENTIC_ACCOUNT` · `review` before
every `place` with blocking-alert skip · `ref_id` idempotency · caps re-checked vs fresh buying
power · daily breaker on broker start-of-day equity · fail-closed snapshot · resting GTC stop on
every whole-share lot · full audit log with order ids.
**Kill switch:** set `TRADING_MODE=paper`, set `LIVE_ARMED=0`, or disconnect the MCP.

**Verified live (2026-06-04):** a full `review → place → confirm → cancel` round-trip on a
non-executing 1-share limit (0 filled, $0 fees) confirmed the path against real Robinhood. The RH
tool JSON shapes are now mapped to the real responses (every result is wrapped in `data`;
`buying_power` is nested under `data.buying_power.buying_power`; quotes are `data.results[].quote`; a
resting stop is identified by a non-null `stop_price`; review alerts live in `data.order_checks`,
where `{}` = clear and a routine `EQUITY_SUITABILITY` disclosure is non-blocking). One operational
prerequisite surfaced: the account's **investor profile must be completed** or `place` 400s before
the second trade.

Pure-logic unit tests (no MCP, no orders): `python3 scripts/test_live_execute.py` (38 assertions).
