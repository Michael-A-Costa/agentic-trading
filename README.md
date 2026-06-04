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

Real money will run through this repo. Read **CLAUDE.md → Critical Rules** first. The operating
model is **autonomous, not per-trade approval** — the seatbelt is code, not a human clicking "yes":

- **Autonomous within one account.** Trades run unattended in the isolated, agentic-allowed account
  (`your_account_number`) with **no per-trade human sign-off**. Every other account is read-only.
- **Safety lives in code**, enforced deterministically in `apply_decision.py` / `tick_context.py`:
  the `.env` caps (`MAX_POSITION_USD`, `MAX_TOTAL_EXPOSURE_USD`, `MAX_SYMBOL_WEIGHT`,
  `MAX_PER_TRADE_LOSS_USD`, `MAX_OPEN_POSITIONS`), per-position stops/take-profits, a daily-loss
  **circuit breaker**, and append-only logging of every decision + fill to `data/`.
- `review_equity_order` precedes `place_equity_order` to catch **broker alerts** (PDT, halts,
  buying power) and to log the preview — it is **not** a human approval gate.
- **Mode is `paper` today** (simulates fills against live quotes; places no real orders). **Live is
  not yet wired into the executor** — the tick engine refuses to run with `TRADING_MODE=live` until
  the real `review → place` path exists. Protective stops are currently **synthetic** (enforced at
  the ~5-min tick when the host is awake), not resting broker orders.
- **Kill switch:** disconnect the `robinhood-trading` MCP, set `TRADING_MODE=paper`, or stop the
  launchd tick.
- Secrets and real account data never get committed (`.env` and `data/` are gitignored).
