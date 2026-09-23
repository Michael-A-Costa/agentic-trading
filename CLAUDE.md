# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Purpose

This is an **agentic trading** workspace — code, strategies, and tooling for researching,
backtesting, and (carefully) executing equity trades through the **Robinhood trading MCP**.

It provides:
- **Trading strategies** — signal generation, position sizing, and risk rules (`strategies/`)
- **Backtest / research scripts** — analysis, data pulls, and offline simulation (`scripts/`)
- **Market & account data** — cached quotes, positions, and run artifacts (`data/`)
- **Claude Code skills** — general-purpose helpers carried over from the dev workspace (`.claude/skills/`)

The Robinhood MCP is registered in `.mcp.json` and exposes account, quote, and order tools
(see **MCP Tools** below). Anything touching real money runs through it.

## Critical Rules

Read these first — they override convenience and apply to every task. This repo moves real
money **autonomously**, so the risk guardrails are non-negotiable.

### Operating Model — Autonomous (authorized)
The account owner (Michael) has **durably authorized fully autonomous trading** in the
isolated, `agentic_allowed=true` account (`your_account_number`). This is a sanctioned *playground*:

- **No per-trade human approval.** Do **not** stop to ask before placing or cancelling orders.
  The standing authorization covers the whole strategy, not one order at a time.
- **Free rein on research & selection.** Do your own due diligence — screen, find names, build
  conviction. **Not** restricted to a fixed watchlist.
- **Safety lives in code, not in human approval.** The seatbelt is the **Risk Guardrails**
  below (hard caps, stop-losses, a daily-loss circuit breaker), full **logging** of every
  decision and fill, and Robinhood's **kill switch** (disconnect the MCP) as the backstop.
- **Scope is the agentic account only.** Trade exclusively `your_account_number`. The other accounts are
  read-only context — **never** place an order against them (the MCP would reject it anyway).

### Risk Guardrails (enforced in code / `.env` — the real safety layer)
- **Honour every limit in `.env`**: `MAX_POSITION_PCT`, `MAX_TOTAL_EXPOSURE_PCT`,
  `MAX_PER_TRADE_LOSS_PCT` (fractions of **live equity**), `STOP_LOSS_PCT`, and
  `DAILY_MAX_LOSS_PCT` (a fraction of **start-of-day** equity, capped at `DAILY_MAX_LOSS_CAP_USD`).
  All are resolved to dollars each tick (`caps.*_USD`). A trade that would breach a cap is **not
  placed** — it's skipped and logged. These are tunable by the owner; do not silently exceed them.
- **Daily-loss circuit breaker.** If realized+unrealized P&L for the day hits the resolved
  `DAILY_MAX_LOSS_USD` (`= min(DAILY_MAX_LOSS_PCT × start-of-day equity, DAILY_MAX_LOSS_CAP_USD)`),
  **halt all new entries for the rest of the session** and log it.
- **Always `review_equity_order` before `place_equity_order`** — not for human sign-off, but to
  catch broker alerts (PDT, halts, buying power) and to log the preview. If review returns a
  **blocking** alert, skip the trade and log the reason.
- **Size from live data.** Compute every order's notional from a **fresh** `get_equity_quotes`
  (and current buying power via `get_portfolio`) pulled immediately before placing. Never size
  off a stale quote.
- **Prefer marketable limits** over naked market orders for price protection; record the price
  and the reasoning in the log.
- **Watch concentration.** Don't let one symbol/sector blow past a sane portfolio weight; the
  exposure caps exist to enforce this.
- **Log everything.** Every decision (including no-trades and skips) and every fill goes to
  `data/` as an append-only record, so P&L and behaviour are auditable after the fact. Two layers:
  the fat per-tick `engine-log.jsonl` (what the engine saw + decided), and a dedicated **trade
  history** — `data/trades.jsonl` + a daily `data/journal/trades-<date>.md` blotter — written by
  `trade_log.py` for every executed fill (paper or live). Read it with `scripts/trade_ledger.py`
  (blotter + FIFO round-trips) and `scripts/pnl_report.py` (realized P&L + exit-type breakdown).
- **No real-money claims you can't back.** Never assert a fill, balance, or P&L number you
  didn't read from a tool. If reasoning from a stale value, say so.

### Git Commit Rules
- Never mention Claude or any AI assistant in commit messages (no `Co-Authored-By` lines, no
  references to AI).
- Write tight, conventional commit messages (`feat:`, `fix:`, `chore:`, `docs:`).

### Secrets
- **Never commit credentials or live account data.** API keys, tokens, account numbers, and
  any real position/balance dumps stay out of git. Use `.env` (gitignored) for secrets and
  `.env.example` for the placeholder template. `data/` is gitignored by default.

### Python Environment
- **Run scripts with `python3`.** Dependencies (`requests`, data/analysis libs, etc.) must be
  on the interpreter `python3` resolves to. If you hit `ModuleNotFoundError`, `python3` is
  likely an older system interpreter — fall back to `python3.11` explicitly for that command,
  or activate the project venv.

### Bash Command Rules
- Avoid `cd`-ing into directories to run commands. Use the path to the script directly
  (e.g. `python3 scripts/backtest.py`, not `cd scripts && python3 backtest.py`). This avoids
  permission prompts from compound commands.
- Never combine `cd` with output redirection (`2>/dev/null`) in one command — it trips a
  Claude Code security check. Drop the redirection and let errors show.

## MCP Tools — `robinhood-trading`

Registered in `.mcp.json` (`https://agent.robinhood.com/mcp/trading`). Schemas load on
demand. Read tools are free; the two write tools execute autonomously within the **Risk
Guardrails** above (no human approval — caps + logging are the gate).

The server grew from 10 tools at project start to **77** (catalogued 2026-09-22). The engine
still uses only the equity rows marked *engine*; everything else is available for research/DD
and future strategies. All names are prefixed `mcp__robinhood-trading__`.

| Group | Tools | Kind |
|------|------|------|
| Account (*engine*) | `get_accounts`, `get_portfolio`, `get_equity_positions`, `get_equity_orders`, `get_equity_tradability`, `search` | read |
| Quotes (*engine*) | `get_equity_quotes` | read |
| Equity orders (*engine*) | `review_equity_order` (pre-trade review, no execution) | read |
| Equity orders (*engine*) | `place_equity_order`, `cancel_equity_order` | **write — auto, capped** |
| Market data | `get_equity_historicals` (OHLCV bars), `get_equity_technical_indicators` (RSI/MACD/BB/MA/ATR/VWAP), `get_equity_price_book` (L2, ≤4 symbols), `get_indexes`, `get_index_quotes`, `get_index_historicals` | read |
| Fundamentals / catalysts | `get_equity_fundamentals`, `get_financials`, `get_earnings_calendar` (≤31d window), `get_earnings_results`, `get_equity_analyst_ratings`, `get_equity_news`, `get_politician_trades` | read |
| SEC filings | `get_sec_filing_index`, `get_sec_filing`, `get_sec_filing_facts`, `get_sec_filing_facts_catalog` | read |
| Scanner | `get_scanner_filter_specs`, `get_scanner_datapoints`, `preview_scan` (ad-hoc, unsaved), `get_scans`, `run_scan` | read |
| Scanner (saved) | `create_scan`, `update_scan_filters`, `update_scan_config` | write (config only) |
| P&L / lots | `get_realized_pnl`, `get_pnl_trade_history` (broker-side realized P&L), `get_equity_tax_lots` | read |
| Upgrades | `get_option_level_upgrade_info` (L2 = long options/CC/CSP, works on cash; L3 = spreads, needs margin/limited margin), `get_limited_margin_upgrade_info`, `get_crypto_account_onboarding_info` | read |
| Options | `get_option_chains`, `get_option_instruments`, `get_option_quotes`, `get_option_historicals`, `get_option_positions`, `get_option_orders` | read |
| Options orders | `review_option_order` (read) → `place_option_order`, `cancel_option_order`, `exercise_option`, `cancel_option_exercise` | **write — NOT wired** |
| Crypto | `get_currency_pairs`, `get_crypto_quotes`, `get_crypto_positions`, `get_crypto_orders` | read |
| Crypto orders | `preview_crypto_order` (read) → `place_crypto_order`, `cancel_crypto_order` | **write — NOT wired** |
| Alerts | `get_alerts`, `get_alert_log` (read); `create_alert`, `update_alert`, `delete_alert`, `mark_alerts_read` | write (alerts only) |
| Watchlists | `get_watchlists`, `get_watchlist_items`, `get_option_watchlist`, `get_popular_watchlists` (read); `create_watchlist`, `update_watchlist`, `add_to_watchlist`, `remove_from_watchlist`, `add_option_to_watchlist`, `remove_option_from_watchlist`, `follow_watchlist`, `unfollow_watchlist` | write (lists only) |

**NOT wired** = no Python guardrail layer exists for that asset class yet; don't place options or
crypto orders until one is built (caps, review→place, logging), the same way equities are.
Options need the agentic account's `option_level` raised (it's empty as of 2026-09-22); crypto
uses the linked crypto account (`rhs_account_number` for crypto-backed calls).

Autonomous flow for a trade idea: DD / `search` / `get_equity_tradability` →
`get_equity_quotes` → `get_portfolio` (buying power) → size within `.env` caps →
`review_equity_order` (alert/log check) → `place_equity_order` → log the decision + fill to
`data/`. Skip + log if a cap or a blocking broker alert would be hit.

### Active strategy — BTC trend sleeve (since 2026-09-23)
Plan + evidence: `docs/strategy-redesign-2026-09.md` (bake-off: `scripts/bakeoff_*.py`,
`data/bakeoff/*_results.md`). Hold **IBIT** while BTC's last completed daily close (Coinbase public
candles — not RH's marked-up crypto quote) is above its 50-day SMA; cash below. Vol-targeted,
risk-sized (`position = equity × TREND_RISK_BUDGET_PCT ÷ TREND_STOP_PCT`), whole shares only, **every
lot carries a resting GTC stop 8% below entry** (never lowered; synthetic market-sell fallback).
Re-entry after a stop-out only on a fresh cross; breaker at `TREND_BREAKER_DD_PCT` below the equity
high-water mark. All rules in `scripts/trend_sleeve.py` (tests: `test_trend_sleeve.py`) — no LLM.

- Schedule: `run_trend.sh open` 09:45 ET + `run_trend.sh close` 15:50 ET, weekdays (launchd
  `com.agentic.trend-open` / `-close`). Nothing intraday; the resting stop covers the gaps.
- Gate: `TREND_ARMED!=1` = dry-run (real `review_equity_order`, places nothing). State
  `data/trend_state.json`, per-run log `data/trend-log.jsonl`, fills → `data/trades.jsonl` tagged `[btc-trend]`.
- Kill switches: unload the two `com.agentic.trend-*` plists → `TREND_ARMED=0` → disconnect the MCP.
- Options and RH crypto orders remain unwired (bake-off: no edge over buy-and-hold / ~1.9% RH crypto round trip).

### Retired — intraday momentum engine (below, kept for reference)
Stopped 2026-09-23: no intraday edge (see memory). Its launchd agents were removed from
`~/Library/LaunchAgents` (templates remain in `scripts/`), and `LIVE_ARMED=0`. Don't reload it
without the owner's say-so.

### Execution pipeline (how a tick runs) — retired engine
Mode is selected by **which entry script you run** — there is no `TRADING_MODE` dispatch.
Each script forces its own mode after sourcing `.env`, so the wrong `.env` value can't
accidentally flip modes.

**Paper** (simulated, default): `run_paper_tick.sh` (launchd via `com.agentic.trading-paper.plist`,
every 15 min) + `run_paper_sentinel.sh` (1 min). Flow: market regime →
`tick_context.py` (deterministic screen + gate) → `decide.py` (Stage-2 DD) →
`apply_decision.py` (simulated fill, tracks `data/paper_state.json`). No real orders.

**Live** (real money): `run_live_tick.sh` (launchd via `com.agentic.trading-live.plist`,
every 5 min) + `run_live_sentinel.sh` (1 min). Flow: precheck → market regime →
`broker_snapshot.py` → `live_tick_context.py` (context + gate) → `decide.py` →
`live_execute.py` (real `review → place` via the MCP relay agent `rh_mcp.py`). All
sizing/cap/gating logic stays in Python; the agent only relays. Truth is re-read from the
broker; `data/live_state.json` holds only our stop/TP metadata. Whole-share lots →
`limit` entry + resting `stop_market` GTC; fractional → `market` + synthetic stop.

**Live is double-gated:** running `run_live_tick.sh` with `LIVE_ARMED!=1` is a **dry-run**
(real `review`, logs intended orders, places nothing); `LIVE_ARMED=1` actually places (entries conviction-tiered at 1.0×/0.6×/0.35× of
`MAX_POSITION_USD`, within the exposure/settled-cash caps). **Kill switches** (in
order of preference):
1. `launchctl unload com.agentic.trading-live.plist` — stops the live scheduler
2. `LIVE_ARMED=0` in `.env` — arms nothing new (dry-run mode)
3. Disconnect the Robinhood MCP — blocks all relay calls

The live path is built and verified end-to-end against real Robinhood (2026-06-04). Note:
the agentic account's **investor profile must be completed** or `place` 400s before the
second trade.

## Directory Structure

```
agentic-trading/
├── .claude/skills/   # Claude Code skill definitions (slash commands)
├── .mcp.json         # Robinhood trading MCP registration
├── strategies/       # Trading strategies — signals, sizing, risk rules
├── scripts/          # Backtest / research / data-pull scripts
└── data/             # Cached market + account data, run artifacts (gitignored)
```

## Available Skills (Slash Commands)

Skills live in `.claude/skills/` and are surfaced to Claude Code each session — carried over
from the dev workspace as general-purpose helpers:

- **`/save`** — save conversation responses to a markdown file.
- **`/session-doctor`** — audit the current session for process mistakes and fix the safe ones.
- **`/convert-doc`** — convert between Markdown and PDF.
- **`/caveman`** — ultra-compressed, token-efficient output mode.

Add trading-specific skills under `.claude/skills/<name>/SKILL.md` as the strategy/backtest
tooling grows.

## Credentials

```bash
cp .env.example .env   # then edit with your values
```

Secrets live in `.env` (gitignored). Robinhood MCP auth is handled by the MCP server /
`/mcp` connection flow, not by a token in this repo.
