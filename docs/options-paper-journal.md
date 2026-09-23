# Options paper journal — idea (NOT BUILT, 2026-09-23)

Status: **documented idea, not built.** Nothing here places or reviews real orders yet.

## Why
The owner is interested in options income but new to options. The bake-off
(`data/bakeoff/options_results.md`, `docs/strategy-redesign-2026-09.md`) found no options strategy beat
buy-and-hold:
- since 2007, put-write and covered-call indexes had lower Sharpe than the S&P
- income ETFs trailed their underlyings
- the RH single-stock CSP replay lost money

So options stay off the live account. A **paper journal** lets the owner watch real trades play out at
$0 risk. It also builds the forward, out-of-sample record we'd need before any options money goes in.
Our historical replay was limited because RH option history is daily marks only, with no bid/ask or
greeks.

## What it would do
A weekly (plus event-driven) agent run that, for each tracked strategy:
1. **Picks the trade by fixed rules**, not LLM judgment. It pulls the live chain via
   `get_option_chains` / `get_option_instruments` / `get_option_quotes`; those quotes carry
   bid/ask, delta, IV and OI.
2. **Previews it for real** with `review_option_order` (read-only; never `place_option_order`). This
   records the broker's actual price, alerts and collateral.
3. **Journals the "fill"** at the real bid (for sells) or ask (for buys), not the mid, so costs are
   honest.
4. **Manages it daily** with the same rules: take profit, roll, expiry, assignment and stop. Every
   event is marked from live quotes.
5. **Writes a plain-English weekly note** for the owner: what it "did", why, what it would have made
   or lost, and one thing to learn from it (e.g. why the put got assigned).

## Strategies to track side by side
| Strategy | Legs (Level 2 OK) | Underlyings | Question it answers |
|---|---|---|---|
| Cash-secured put | sell 30–45 DTE ~0.25Δ put | IBIT, SOFI, F | Does premium beat holding cash / the stock? |
| Wheel | CSP → covered call ≥ cost basis on assignment | IBIT, SOFI | Does the covered-call leg help or trap? |
| Covered call on the trend lot | own 100 IBIT + sell ~0.25Δ call | IBIT | Does it add income to the trend sleeve without killing the upside? |
| Zero-cost collar | own 100 IBIT + buy ~5% OTM put + sell call to pay for it | IBIT | Is a hard floor better than the 8% stop, which weekend gaps can blow through? |
| Protective put | own 100 IBIT + buy ~5% OTM put | IBIT | What does crash insurance actually cost per year? |
| Bull put spread | sell put + buy lower put | IBIT, SOFI | Defined-risk CSP. **Needs Level 3** (not approved); tracked only to decide whether the upgrade is worth it |

Rule defaults (from practitioner consensus; to be fixed before the journal starts, never tuned mid-run):
- Skip any expiry with earnings before it.
- Skip if the spread is > 10% of mid or OI < 100.
- Take profit at 50%. At 21 DTE, close or roll for a net credit only, and roll at most once.
- Take assignment rather than defend.
- Every position carries a stop rule (owner rule), picked from the bake-off's stop study: buy back a
  short put at 2× the credit.

## Scoreboard (what would justify real money)
- Per strategy: return on collateral, max drawdown, worst single trade, assignment count, unrealized
  P&L on held shares (no "counting only realized premium"), vs holding the underlying and vs T-bills.
- **Pre-registered gate to go live:** ≥ 20 closed cycles for a strategy, with return on collateral
  beating both holding the underlying and T-bills after the real bid/ask, and max drawdown ≤ the
  underlying's.
- The collar and protective-put rows are judged against the trend sleeve's 8% stop, using the same
  weeks of IBIT.

## Build sketch (when the owner says go)
- `scripts/options_journal.py`: pure rule functions (strike/expiry pick, management), unit-tested.
  Read-only MCP calls via `rh_direct`, with `review_option_order` only.
- State: `data/options_journal.json`. Events: `data/options-journal.jsonl`. Weekly note:
  `data/journal/options-<date>.md`.
- launchd: Monday 10:30 ET entry pass + daily 15:30 ET manage pass. Runs ride alongside the trend
  sleeve's check-ins; no minute-level ticking.
- A macOS notification when the weekly note is ready. Reuse `trend_sleeve.notify`, which uses
  AppleScript quoting, not json.dumps.
- Cost: ~15–25 read calls per run, $0 (direct MCP, no LLM needed for the rules; the weekly note can be
  a template, or one cheap model call).

## Open questions
- Does `review_option_order` return usable collateral / buying-power figures for a cash-secured put
  on a cash account? Check on the first run.
- RH option quotes carry greeks live, but option history doesn't. The journal therefore has to run
  forward in time; it can't be backfilled.
- Paper fills at bid/ask are conservative for liquid IBIT chains. They may be optimistic for thin
  SOFI/F strikes.
