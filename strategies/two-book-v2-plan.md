# Two-Book Split (v2) — One Account, Two Virtual Books

**Drafted:** 2026-06-09 · **Status:** PLAN (not implemented)
**Supersedes nothing** — extends `catalyst-drift-v1-plan.md`; the v1 engine keeps running while this lands.

## Why

The 2026-06-09 methodology review found one structural flaw and several smaller ones:

1. **The experiment is contaminated.** The evidence gate ("≥30 resolved qualified-PEAD events show
   no lift → disarm") judges only the PEAD cohort, but most dollars at risk are free-rein
   discretion. A clean PEAD verdict says nothing about the majority of the book; a dirty one
   disarms a strategy the book mostly isn't running.
2. **Universe ≠ validated regime.** The t=3.10 PEAD edge was the LARGE-cap control. The engine
   admits $300M+ names, where our own research found the dirtiest data — gated only by the
   unproven LLM classifier.
3. **Economics unstated.** At ~$2k equity, expected gross edge is ~$4–6/trade vs an ~85s Sonnet
   DD per candidate. Fine as R&D; should be measured, not ignored.
4. Smaller: negative-gap PEAD candidates waste DD slots; exit policy backtested on a tiny fill
   sample; methodology doc duplicates the soft-cut section.

We have exactly **one** agentic account, so the fix is a **virtual split**: two books inside the
same account, sharing all infrastructure, each with its own capital fraction, entry policy,
ledger, and disarm criterion.

## Design

### The two books

| | `pead` book | `disco` book |
|---|---|---|
| **Thesis** | The one measured edge: qualified post-earnings gap-drift | Free-rein agent discretion (the fun sleeve) |
| **Entry eligibility** | `pead_qualified=true` (gap ≥ `GAP_THRESHOLD_PCT` on ≥ `VOL_MULT_MIN`× volume) **AND** mcap ≥ `PEAD_BOOK_MIN_MKTCAP_USD` (match the backtest's LARGE bucket — read the exact cut from `backtest_gap_drift.py` when implementing) | Anything that survives discovery + DD commit and isn't claimed by `pead` |
| **Capital** | `BOOK_PEAD_FRAC` (default 0.50) × live equity | remainder |
| **Disarm criterion** | existing gate: ≥30 resolved events, qualified cohort shows no lift over gap-alone → `BOOK_PEAD_ENABLED=0` | NEW: after ≥30 resolved round-trips, if expectancy < 0 net of costs (or book hits its own −10% tripwire twice) → `BOOK_DISCO_ENABLED=0` |
| **Exits** | unchanged (stop/trail/soft-cut/21d time-exit) | unchanged |

A disarmed book halts **new entries only**; its exits, stops, and sentinel coverage keep running.
Disarming one book does not touch the other. The global tripwire / daily breaker / kill switches
stay account-wide and unchanged.

### Key architectural insight: one planner, two policies

We do **not** run two agents. The planner already runs single-flight under `data/.tick.lock`;
discovery, quotes, regime, DD cache, and stock-memory are naturally shared because there is one
tick. "Two agents that share knowledge and lock tickers" reduces to **one tick with a routing
step** — sharing is free and interference is impossible by construction:

- **Shared (unchanged):** `discover.py` + `discover_pead.py` candidate merge, quote fetches,
  market regime, the per-symbol-per-day DD cache, manage-DD, `stock_memory.py` exclusions,
  sentinel, all logging.
- **Routed (new):** after a DD **commit**, a pure function assigns the book:
  `route_book(dd, meta) → "pead" | "disco"`. PEAD-eligible names go to `pead`; everything else
  to `disco`. If the target book is full/disarmed, the commit is **skipped + logged** — it does
  NOT spill into the other book (spillover would re-contaminate the experiment).
- **Ticker ownership (the "lock"):** each lot carries `book`. A symbol held by one book cannot
  be entered, scaled, or sold by the other; the owner registry is just the `lots` map. Since the
  planner is single-flight there is no concurrency to lock against — ownership is attribution +
  a routing guard, not a mutex.

### Capital model

Virtual book equity is resolved **fresh each tick**, like every other cap:

```
book_equity(pead)  = BOOK_PEAD_FRAC × live_equity
book_equity(disco) = (1 − BOOK_PEAD_FRAC) × live_equity
book_exposure(b)   = Σ market value of lots tagged book=b
book_headroom(b)   = book_equity(b) × MAX_TOTAL_EXPOSURE_PCT − book_exposure(b)
```

- **Auto-rebalancing by construction:** fractions of *current total* equity, so a winning book's
  gains lift both books' ceilings. No per-book cash ledger, no drift to reconcile. (Per-book
  compounding — winners keep their gains — is a v2.1 option; it needs a realized-P&L ledger per
  book, which the `book` field on `trades.jsonl` makes possible later.)
- **Per-name cap stays vs TOTAL equity** (`MAX_POSITION_PCT` unchanged). At $2,064 equity a book
  is ~$1,032; making the per-name cap book-relative would shrink full-size entries to ~$155 and
  the whole-share constraint would reject most of the universe. Consequence to accept: one name
  can be ~30% of its book. The book exposure ceiling (~$929/book at 90%) still binds at ~3
  full-size positions per book.
- **Settled-cash guard stays GLOBAL** — the broker has one cash balance; both books draw from it
  first-come. `MAX_OPEN_POSITIONS` splits per book (`BOOK_MAX_POSITIONS`, default 15/15).
- Daily breaker and global tripwire are unchanged and account-wide (safety stays simple).

### New `.env` knobs

```bash
BOOKS_ENABLED=1                  # master switch; 0 = exactly today's behavior
BOOK_PEAD_FRAC=0.50              # pead book fraction of live equity (disco gets the rest)
PEAD_BOOK_MIN_MKTCAP_USD=...     # LARGE bucket cut from backtest_gap_drift.py
BOOK_PEAD_ENABLED=1              # disarm flags — halt new entries for one book only
BOOK_DISCO_ENABLED=1
BOOK_TRIPWIRE_PCT=10             # per-book soft tripwire vs that book's share of LIVE_TRIPWIRE_BASELINE_USD
BOOK_MAX_POSITIONS=15            # per-book position-count cap
```

## File-mapped changes

1. **`scripts/decide.py`** — `route_book(dd, pead_meta) → str` after a commit verdict; stamp
   `book` into the decision payload. DD cache stays keyed per symbol+day (shared). No prompt
   change — `pead_qualified` is already produced.
2. **`scripts/live_execute.py`** —
   - `check_entry_caps`: add `book_exposure + notional ≤ BOOK_EXPOSURE_USD[book]` and the
     per-book position count; keep all existing global checks (mirror in
     `apply_decision.validate_and_fill` for paper parity, per the existing keep-in-sync note at
     `live_execute.py:265`).
   - `execute_buy`: refuse entry if the symbol's existing lot belongs to the other book
     (skip + log `book_conflict`); stamp `book` on the lot.
   - `execute_sell` / reconcile / trail: operate on the lot's own book; behavior otherwise
     unchanged.
   - Per-book tripwire check alongside the global one in `main()`.
3. **`scripts/live_tick_context.py`** — resolve `BOOK_*_USD` caps from live equity each tick and
   put per-book exposure/headroom into the context the DD prompt sees (so the agent knows which
   book has room).
4. **`scripts/trade_log.py`** — `book` field on every `trades.jsonl` record and the daily blotter.
5. **`scripts/catalyst_log.py` / `catalyst_filter_report.py`** — carry `book` on events; report
   gains a per-book split (the qualified-PEAD vs gap-alone lift table becomes the `pead` book's
   verdict; a new expectancy-net-of-costs table becomes the `disco` book's verdict).
6. **`scripts/pnl_report.py` / `trade_ledger.py`** — `--by-book` grouping.
7. **`scripts/discover_pead.py`** — negative-gap pre-filter (`PEAD_DIRECTION=up`): drop reporters
   whose post-earnings move is below 0 before they consume DD slots (long-only account can never
   trade them). Keep them in `pead_meta` for labeling; just don't surface as candidates.
8. **State migration (one-shot)** — tag existing `live_state.json` lots: `book="disco"` for all
   current holdings (none were qualified-PEAD entries; CPB has `pead_qualified=null`). New lots
   always get a book at entry.
9. **Docs** — `docs/methodology.md`: de-duplicate the soft-cut section (§6 describes it twice);
   add a "Two books" section; state plainly that the PEAD edge is validated LARGE-cap only and
   that at current equity the account is an R&D vehicle whose deliverable is the forward ledger.

## Other improvements (from the review, not book-related)

- **DD cost ledger:** `decide.py` already records per-call usage (`_record_usage` /
  `usage_summary`). Persist a daily roll-up to `data/costs.jsonl` and print a
  *gross-edge-vs-token-spend* line in `pnl_report.py`. The methodology's honesty rule should
  extend to costs: if tokens > gross edge, say so in the report, weekly.
- **Exit-policy revalidation:** the 8%-softcut/12%-stop/+20%-trail cell came from a tiny fill
  sample. Re-run `backtest_exit_policy.py` once the ledger holds ≥30 round-trips (lands near the
  2026-06-26 evidence checkpoint — do both in one sitting).
- **Fractional policy:** whole-share rounding already covers entries (`size_entry`); fractional
  lots can still exist from legacy/partial fills. Document that fractional = synthetic-stop-only
  (no overnight protection) and prefer exiting fractional remnants at the next opportunity.

## Phasing (each phase shippable alone)

- **Phase 0 — measure, don't act** (lowest risk, do first): `book` routing computed and stamped
  on decisions/lots/trades + report splits (items 1, 4, 5, 6, 8) with `BOOKS_ENABLED=0` so no
  cap behavior changes; negative-gap filter (7); doc fixes (9). After a few days the per-book
  ledgers exist retroactively-forward and we can sanity-check routing before it has teeth.
- **Phase 1 — enforce:** flip `BOOKS_ENABLED=1`: per-book exposure/position caps, no-spillover
  routing, ticker ownership guard (items 2, 3). Verify with a dry-run session (`LIVE_ARMED=0`)
  before arming.
- **Phase 2 — judge:** per-book tripwires + disarm flags wired into `live_execute`; DD cost
  ledger; scheduled 2026-06-26 checkpoint reads BOTH books' verdicts (PEAD lift table; disco
  expectancy net of costs) and the exit-policy re-backtest.

## Open decisions (owner)

1. **Split fraction** — default 0.50/0.50. Argument for 0.40 pead / 0.60 disco: qualified-PEAD
   candidates are scarce (the in-cash problem that triggered the free-rein pivot), so the pead
   book may sit partly idle; disco generates resolved trades faster, maturing its ledger sooner.
   Argument for 0.60 pead: it's the only measured edge. 50/50 is the defensible default.
2. **LARGE-cap floor for the pead book** — read the bucket cut out of `backtest_gap_drift.py`
   and use exactly that number; don't invent a rounder one.
3. **Disco disarm rule** — proposed: ≥30 resolved round-trips with negative net expectancy, or
   two per-book tripwire hits. Owner may prefer looser (it's the fun sleeve) — but it should be
   pre-committed in writing either way, like the PEAD gate was.
