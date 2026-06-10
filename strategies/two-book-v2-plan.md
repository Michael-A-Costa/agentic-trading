# Two-Book Split (v2) — One Account, Two Virtual Books

**Drafted:** 2026-06-09 · **Status:** IMPLEMENTED 2026-06-09 (all phases' code landed; tests green).
**Rollout state:** `BOOKS_ENABLED=0` (Phase 0 — tagging/ledgers live, enforcement dormant). Per the
phasing below: sanity-check the routing in the ledgers for a few days, then flip `BOOKS_ENABLED=1`
and verify one `LIVE_ARMED=0` dry-run session before the enforcement carries real orders. Existing
live lots + paper positions were migrated to `book=disco` on 2026-06-09.
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
| **Entry eligibility** | `pead_qualified=true` (gap ≥ `GAP_THRESHOLD_PCT` on ≥ `VOL_MULT_MIN`× volume) **AND** mcap ≥ `PEAD_BOOK_MIN_MKTCAP_USD` ($30B proxy — see Resolved decisions #2) | Anything that survives discovery + DD commit and isn't claimed by `pead` |
| **Capital** | **Ceiling, not allocation:** up to `BOOK_PEAD_MAX_FRAC` (0.30) × live equity when qualified candidates exist; reserves **nothing** while idle | Everything pead isn't actually using, up to the global 90% exposure ceiling |
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
  to `disco`. If the target book is at its ceiling or disarmed, the commit is **skipped +
  logged** — the *label* never spills (a pead-qualified name is never re-tagged disco just to
  get funded; that would re-contaminate the experiment). Capital, by contrast, IS shared — see
  the capital model below.
- **Ticker ownership (the "lock"):** each lot carries `book`. A symbol held by one book cannot
  be entered, scaled, or sold by the other; the owner registry is just the `lots` map. Since the
  planner is single-flight there is no concurrency to lock against — ownership is attribution +
  a routing guard, not a mutex.

### Capital model — shared pool, ceilings + priority (NOT a partition)

**Owner constraint (2026-06-09): no idle reserve.** The pead book must never leave 30% of the
account sitting in cash waiting for earnings season. The experiment doesn't need fenced capital
— it needs correctly **tagged trades**; the lift verdict counts events per cohort, not dollars.
So capital is one shared pool with ceilings and an ordering rule:

```
book_exposure(b)      = Σ market value of lots tagged book=b
pead_headroom         = min(BOOK_PEAD_MAX_FRAC × live_equity − book_exposure(pead),
                            MAX_TOTAL_EXPOSURE_USD − total_exposure)
disco_headroom        = MAX_TOTAL_EXPOSURE_USD − total_exposure      # no fence of its own
```

- **Disco can always deploy up to the full global 90% ceiling.** When the PEAD calendar is
  quiet, the account behaves exactly as it does today — nothing sits idle.
- **Pead has a ceiling (30% of equity) and first claim.** Within a tick, pead-routed entries
  are processed **before** disco entries, so when both want the last settled dollar, pead wins.
- **Accepted consequence:** a qualified PEAD candidate can appear while disco has the account
  fully deployed, and go unfunded. That costs a *trade*, not the *experiment* — the candidate
  and the agent's verdict still land in `catalyst_log`, and the forward-lift ledger measures
  evaluated events whether or not a fill happened. Log these as `pead_unfunded` so we can count
  how often it actually bites; if it's frequent, an optional earnings-season cash reserve is
  the v2.1 answer (knob exists below, default OFF).
- **Per-name cap stays vs TOTAL equity** (`MAX_POSITION_PCT` unchanged) — book-relative caps
  would shrink full-size entries below the whole-share constraint at current equity.
- **Settled-cash guard stays GLOBAL** — one broker cash balance; the priority ordering above is
  the only arbitration. `MAX_OPEN_POSITIONS` stays global too (no per-book split needed when
  capital is shared; pead's ceiling implies ~2 full-size positions max anyway).
- Daily breaker and global tripwire are unchanged and account-wide (safety stays simple).
- **Per-book tripwires become P&L-based** (a slice-of-equity baseline makes no sense without a
  partition): a book trips when its cumulative net P&L (realized from `trades.jsonl` + open
  unrealized) falls below −`BOOK_TRIPWIRE_PCT`% of its ceiling share of
  `LIVE_TRIPWIRE_BASELINE_USD`. At the $2,064 baseline: pead trips at ≈ −$62 cumulative,
  disco at ≈ −$145.

### New `.env` knobs

```bash
BOOKS_ENABLED=1                  # master switch; 0 = exactly today's behavior
BOOK_PEAD_MAX_FRAC=0.30          # pead exposure CEILING (fraction of live equity). NOT a reserve —
                                 #   idle pead capital is always available to disco
PEAD_BOOK_MIN_MKTCAP_USD=30000000000  # $30B — proxy for the backtest's fixed mega-cap list
BOOK_PEAD_ENABLED=1              # disarm flags — halt new entries for one book only
BOOK_DISCO_ENABLED=1
BOOK_TRIPWIRE_PCT=10             # per-book P&L tripwire: book cum. net P&L <= -10% of its
                                 #   ceiling share of LIVE_TRIPWIRE_BASELINE_USD -> halt that book
PEAD_SEASON_RESERVE_PCT=0        # OPTIONAL (default OFF): settled-cash % held back when the
                                 #   earnings calendar is heavy; only add if pead_unfunded is frequent
```

## File-mapped changes

1. **`scripts/decide.py`** — `route_book(dd, pead_meta) → str` after a commit verdict; stamp
   `book` into the decision payload. DD cache stays keyed per symbol+day (shared). No prompt
   change — `pead_qualified` is already produced.
2. **`scripts/live_execute.py`** —
   - `check_entry_caps`: pead entries additionally checked against
     `BOOK_PEAD_MAX_FRAC × equity`; disco entries only against the existing global exposure
     ceiling. Process pead-routed buys before disco buys within the tick (first claim on
     settled cash). Keep all existing global checks (mirror in
     `apply_decision.validate_and_fill` for paper parity, per the existing keep-in-sync note at
     `live_execute.py:265`). Log `pead_unfunded` when a qualified commit is skipped for lack
     of settled cash.
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

### Considered and deferred: pivot-into-pead (owner question, 2026-06-09)

Question: when a qualified PEAD setup appears and the account is fully deployed, may the engine
sell a green/neutral disco position to fund it? **Deferred — not built.** Reasons:
1. **T+1 makes same-day pivots impossible by design:** sale proceeds are unsettled until the
   next day and `CASH_SETTLEMENT_GUARD` (correctly) refuses to size against them — a same-day
   sell→buy is the textbook GFV pattern. Only a two-step (sell today, enter tomorrow) works;
   tolerable for PEAD's 10–20d window, but it's cutting a position today for a maybe tomorrow.
2. **Wrong selection rule:** selling green positions cuts winners mid-drift — the exact failure
   mode the backtested exit policy (trail-at-+20, far TP) was tuned to prevent. Any pivot would
   have to sell the weakest thesis, not the greenest P&L.
3. **Ledger contamination:** a disco exit forced by pead funding lands in disco's round-trips
   and skews the expectancy that drives its disarm rule; it would need a `pivot_funding` exit
   tag excluded from the verdict.
Decision path instead: Phase 0's `pead_unfunded` counter measures how often funding actually
misses → if frequent AND the pead cohort shows lift at the 2026-06-26 checkpoint, enable
`PEAD_SEASON_RESERVE_PCT` (no forced sells, no contamination) → revisit a pivot only if the
reserve proves insufficient.

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

## Resolved decisions (owner, 2026-06-09)

1. **Pead ceiling 0.30, shared pool — no idle reserve (owner, 2026-06-09).** Originally framed
   as a 30/70 partition; the owner rejected leaving 30% of the account idle between earnings
   seasons. Final model: pead may hold *up to* 30% of equity and gets first claim on settled
   cash within a tick, but reserves nothing while idle — disco can always deploy to the full
   global ceiling. Rationale for the small pead ceiling stands: qualified candidates are scarce
   (earnings-season-clustered; mega-caps rarely gap ≥7% on 2× volume) and each trade holds
   10–20 days, so the pead ledger matures slowly regardless of capital — the gate counts
   *events*, not dollars.
2. **PEAD book floor: `PEAD_BOOK_MIN_MKTCAP_USD=30e9` (proxy).** The backtest's LARGE universe
   was NOT a market-cap cut — it is a fixed 60-name mega-cap list (`backtest_gap_drift.py:52`,
   S&P-100-class names, all roughly ≥$40B). $30B is a deliberate proxy for "that class of
   heavily-covered mega-cap"; it is written down here as a proxy so nobody later mistakes it
   for a backtested threshold.
3. **Disco disarm rule — adopted as proposed:** after ≥30 resolved round-trips, if expectancy
   net of costs is negative, OR the per-book P&L tripwire (disco cumulative net P&L ≤ ≈ −$145
   at the current baseline) fires twice, `BOOK_DISCO_ENABLED=0` (new entries halt; exits keep
   running). Pre-committed now,
   before results exist, for the same reason the PEAD gate was: the kill threshold must be set
   while we don't know the answer, or sunk-cost reasoning will set it later.
