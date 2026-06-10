# agentic-trading

An agentic trading workspace — research, backtest, and (carefully) execute equity trades
through the Robinhood trading MCP, driven by Claude Code.

---

> ## ⚠️ THIS IS NOT FINANCIAL ADVICE
>
> **This is an experimental, personal research project — not investment advice, not a
> recommendation, and not a solicitation to buy or sell any security.**
>
> - Nothing in this repository (code, strategies, logs, commit messages, or docs) is financial,
>   investment, legal, or tax advice. The author is **not** a registered investment adviser or
>   broker-dealer.
> - This software trades **real money autonomously** and **can and will lose money**. Past or
>   simulated performance says nothing about future results. Most of the strategy ideas here have
>   been found to have **little or no edge** (see `data/` memory notes and `docs/`).
> - It is provided **"as is", with no warranty** of any kind and **no guarantee** of correctness,
>   profitability, or fitness for any purpose. You assume **all** risk.
> - It is wired to **one specific, isolated brokerage account** and tuned to its owner's risk
>   tolerance. Do **not** point it at an account whose loss you can't absorb.
> - If you run anything here, **you alone are responsible** for the trades, the losses, and any
>   regulatory or tax consequences. Do your own research; consult a licensed professional.

---

## Layout

```
strategies/   # signals, position sizing, risk rules
scripts/      # tick engine, research / data-pull, backtests
data/         # cached market + account data, run logs (gitignored)
docs/         # methodology + architecture notes
.claude/      # Claude Code skills + local settings
.mcp.json     # Robinhood trading MCP registration
CLAUDE.md     # operating guide + trading safety rules for Claude Code
```

## Setup

```bash
cp .env.example .env   # fill in risk limits / knobs
```

The Robinhood MCP is registered in `.mcp.json`. Connect/authenticate it from Claude Code
with `/mcp` (`robinhood-trading`). The engine also reads that same authenticated session
**directly** (no LLM) for fast, deterministic broker reads — see `scripts/rh_direct.py`.

## How it runs

Two scheduled loops per mode (launchd), selected by **which entry script runs** — there is no
mode flag to flip by accident:

- **Paper** (default, simulated fills): `run_paper_tick.sh` every 15 min + `run_paper_sentinel.sh`
  every 1 min. Flow: market regime → `tick_context.py` (screen + gate) → `decide.py` (deep DD) →
  `apply_decision.py` (simulated fill).
- **Live** (real orders): `run_live_tick.sh` + `run_live_sentinel.sh`. Flow: precheck →
  `broker_snapshot.py` → `live_tick_context.py` → `decide.py` → `live_execute.py`
  (`review → place` via the MCP). **Live is double-gated:** it only places when `LIVE_ARMED=1`;
  otherwise it dry-runs (real review, places nothing).

## Safety

Real money runs through this repo. Read **CLAUDE.md → Critical Rules** first. The operating
model is **autonomous, not per-trade approval** — the seatbelt is code, not a human clicking "yes":

- **Autonomous within one account.** Trades run unattended in the isolated, agentic-allowed
  account with **no per-trade human sign-off**. Every other account is read-only.
- **Safety lives in code**, enforced deterministically: the `.env` caps (`MAX_POSITION_PCT`,
  `MAX_TOTAL_EXPOSURE_PCT`, `MAX_PER_TRADE_LOSS_PCT`, `STOP_LOSS_PCT`, `MAX_OPEN_POSITIONS`),
  per-position stops/take-profits, a daily-loss **circuit breaker** (`DAILY_MAX_LOSS_PCT`), a
  cash-account settled-funds guard, and append-only logging of every decision + fill to `data/`.
- `review_equity_order` precedes `place_equity_order` to catch **broker alerts** (PDT, halts,
  buying power) and to log the preview — it is **not** a human approval gate.
- **Kill switches** (in order of preference): `launchctl unload` the live plist; set `LIVE_ARMED=0`
  (arms nothing, dry-run only); or disconnect the `robinhood-trading` MCP.
- Secrets and real account data never get committed (`.env` and `data/` are gitignored).

## Disclaimer (again)

By using, running, or modifying this code you acknowledge it is **not financial advice**, comes
with **no warranty**, and that **any losses are your own**. See the banner at the top.
