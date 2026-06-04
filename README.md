# agentic-trading

An agentic trading workspace — research, backtest, and (carefully) execute equity trades
through the Robinhood trading MCP, driven by Claude Code.

## Layout

```
strategies/   # signals, position sizing, risk rules
scripts/      # backtest / research / data-pull scripts
data/         # cached market + account data (gitignored)
.claude/      # Claude Code skills + local settings
.mcp.json     # Robinhood trading MCP registration
CLAUDE.md     # operating guide + trading safety rules for Claude Code
```

## Setup

```bash
cp .env.example .env   # fill in risk limits / mode
```

The Robinhood MCP is registered in `.mcp.json`. Connect/authenticate it from Claude Code
with `/mcp` (`robinhood-trading`).

## Safety

Real money runs through this repo. Read **CLAUDE.md → Critical Rules** first:

- No order is placed, modified, or cancelled without explicit per-order approval.
- `review_equity_order` always precedes `place_equity_order`; the reviewed order is shown
  before anyone says "yes".
- Default to paper / dry-run; live trading is opt-in per request.
- Secrets and real account data never get committed.
