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

Read these first — they override convenience and apply to every task. This repo can move
real money, so the trading guards are non-negotiable.

### Trading Safety Guards
- **Never place, modify, or cancel an order without explicit, in-the-moment user approval.**
  Approval for one order does **not** carry to the next. The global rule against executing
  trades on the user's behalf applies here in full — `place_equity_order` and
  `cancel_equity_order` are confirm-first, every time.
- **Always `review_equity_order` before `place_equity_order`.** Show the user the reviewed
  order (symbol, side, qty, type, limit/stop, estimated cost, buying-power impact) and wait
  for a clear "yes" on *that specific order* before placing it.
- **Read tools are free; write tools are gated.** `get_accounts`, `get_portfolio`,
  `get_equity_positions`, `get_equity_quotes`, `get_equity_orders`,
  `get_equity_tradability`, `search`, and `review_equity_order` are safe to call freely.
  `place_equity_order` and `cancel_equity_order` are not.
- **State the dollars.** Whenever proposing a trade, surface notional value and resulting
  position size / portfolio weight. Never propose an order whose size you have not actually
  computed from live quotes and current buying power.
- **Quote freshness.** Prices move. Re-pull `get_equity_quotes` immediately before building
  any order; do not size a trade off a stale quote from earlier in the session.
- **Default to paper / dry-run.** When backtesting or developing a strategy, simulate. Only
  reach for live order tools when the user explicitly asks to trade live.
- **No real-money claims you can't back.** Never assert a fill, a balance, or a P&L number
  you didn't read from a tool this session. If you're reasoning from a stale value, say so.

### Risk Discipline
- Respect any position-size, per-trade-loss, and total-exposure limits defined in the
  active strategy or `.env`. If a proposed trade would breach a configured limit, stop and
  flag it rather than placing it.
- Prefer **limit** orders over market orders unless the user asks otherwise; name the limit
  price and the reasoning.
- Surface concentration risk: if a trade pushes a single symbol or sector past a sane weight,
  say so before proposing it.

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
demand. Read tools are safe; the two write tools are confirm-first (see Trading Safety Guards).

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
| `mcp__robinhood-trading__place_equity_order` | **write — confirm first** | Place an order |
| `mcp__robinhood-trading__cancel_equity_order` | **write — confirm first** | Cancel an order |

Typical safe flow for a trade idea: `search` / `get_equity_tradability` → `get_equity_quotes`
→ `get_portfolio` (buying power) → size the order → `review_equity_order` → **ask the user** →
`place_equity_order` only on explicit approval.

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
