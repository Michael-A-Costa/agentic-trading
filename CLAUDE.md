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

| Tool | Kind | Purpose |
|------|------|---------|
| `mcp__robinhood-trading__get_accounts` | read | List brokerage accounts |
| `mcp__robinhood-trading__get_portfolio` | read | Portfolio value / buying power |
| `mcp__robinhood-trading__get_equity_positions` | read | Current equity holdings |
| `mcp__robinhood-trading__get_equity_quotes` | read | Live quotes for symbols |
| `mcp__robinhood-trading__get_equity_orders` | read | Open / historical orders |
| `mcp__robinhood-trading__get_equity_tradability` | read | Whether a symbol is tradable |
| `mcp__robinhood-trading__search` | read | Search instruments |
| `mcp__robinhood-trading__review_equity_order` | read | Pre-trade review (no execution) |
| `mcp__robinhood-trading__place_equity_order` | **write — auto, capped** | Place an order |
| `mcp__robinhood-trading__cancel_equity_order` | **write — auto, capped** | Cancel an order |

Autonomous flow for a trade idea: DD / `search` / `get_equity_tradability` →
`get_equity_quotes` → `get_portfolio` (buying power) → size within `.env` caps →
`review_equity_order` (alert/log check) → `place_equity_order` → log the decision + fill to
`data/`. Skip + log if a cap or a blocking broker alert would be hit.

### Execution pipeline (how a tick runs)
Mode is selected by **which entry script you run** — there is no `TRADING_MODE` dispatch.
Each script forces its own mode after sourcing `.env`, so the wrong `.env` value can't
accidentally flip modes.

**Paper** (simulated, default): `run_paper_tick.sh` (launchd via `com.agentic.trading-paper.plist`,
every 15 min) + `run_paper_sentinel.sh` (1 min). Flow: market regime →
`tick_context.py` (deterministic screen + gate) → `decide.py` (Stage-2 DD) →
`apply_decision.py` (simulated fill, tracks `data/paper_state.json`). No real orders.

**Live** (real money): `run_live_tick.sh` (launchd via `com.agentic.trading-live.plist`,
every 15 min) + `run_live_sentinel.sh` (1 min). Flow: precheck → market regime →
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
