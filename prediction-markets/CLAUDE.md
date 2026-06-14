# CLAUDE.md — `prediction-markets/` (scope override for this subtree)

> The repo-root `CLAUDE.md` governs the **live Robinhood cash-equities** book and
> says "trade exclusively account `your_account_number`, equities only." That mandate does
> **not** apply here, and nothing here is part of it. This file scopes the subtree.

## What this is
Research into **prediction-market** mispricing (Kalshi first; Polymarket notes for
reference). The active question: do near-certain favorites (≥0.90) ending soon carry a
**fee-survivable** edge, or is it negative-skew "pennies in front of a steamroller"?
See `docs/polymarket-drift-v0-plan.md` for the pre-registered hypotheses and gates.

## Hard rules for this subtree
- **Research only. No capital, no live execution, no wallet, no order placement.**
  Public read-only market data (e.g. `api.elections.kalshi.com`) is the ceiling until
  the owner clears **Gate 3** in the plan.
- **Separate rails — never share the equities surface.** Do not import the Robinhood MCP,
  the `.env` at repo root, the live `data/` state, the launchd plists, or any kill switch
  from the equities engine. This venue gets its *own* secrets, scheduler, and kill switch
  if/when it ever goes live.
- **Same backtest discipline as the equities book.** Pre-register hypotheses; score on
  depth/ask-aware fills net of fees (no mid-price); report t-stats **and** the
  drop-top-N-winners stress test; include voided/disputed markets. An edge that only
  survives on mid-price or by excluding upsets is an artifact.
- **Graduation trigger → its own repo.** The moment this needs funding or live execution
  (Gate 3), split it out: independent secrets, scheduler, kill switch, and git lifecycle.
  Until then it stays a subfolder.

## Layout
- `scripts/` — data pulls + backtests (stdlib-only where practical; `python3`).
- `docs/` — research notes / pre-registered plans.
- `data/` — gitignored research artifacts (kept separate from the equities `data/`).
