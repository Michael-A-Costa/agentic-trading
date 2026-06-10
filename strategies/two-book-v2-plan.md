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

---

# v2.1 Addendum — Per-Book Exit Profiles (hybrid: drift vs quick-win)

**Drafted:** 2026-06-10 · **Status:** PROPOSED (design only; owner reviewing before any code).
**Extends** the v2 split above. v2 routes by *provenance* and shares one exit policy
("Exits: unchanged" for both books, §Design table line 40). This addendum makes the **exit policy
itself book-aware**, so the two books run two *return profiles* — without touching the entry
routing, capital model, or disarm rules already specified.

## Why (owner intent, 2026-06-10)

The owner wants three things from the account: **(1) downside protection, (2) quick wins,
(3) a little money along the way.** The backtested exit policy optimises the *opposite* profile —
fewer/bigger/slower/bumpier — because that is what the one validated edge (catalyst gap-drift)
requires. These are genuinely different objectives. Rather than detune the proven edge, split the
objective by book, which we already have the substrate for:

| Book | Profile | Rationale |
|---|---|---|
| `pead` | **Let winners run** (today's exits, unchanged) | The *only* universe where a multi-day edge is validated (t≈3.1 LARGE-cap). Patience is earned here. |
| `disco` | **Quick wins + bank along the way + protection** | The names with **no** validated multi-day edge (all of 2026-06-10's fills). Harvest fast *because* the hold can't be trusted. Serves owner goals 2 & 3; goal 1 is the shared layer below. |

This is the honest version of the hybrid: apply patience only where there is evidence for it.

## Shared vs per-book — the split of the exit schedule

**Shared / global (downside protection — both books want it, unchanged):**
- `STOP_LOSS_PCT=12` — catastrophe stop.
- `SOFT_CUT_PCT=8` + `HOLD_RISK_SELL=1` — cut a loser that's deep **and** still falling (`hold_risk.py`).
- `TRAIL_BREAKEVEN_AT_PCT=12` — lift stop to entry once peak ≥+12% (set 2026-06-10; backtest-neutral
  on pead, pure insurance on disco). Goal 1 is therefore **already live for both books today.**

**Per-book (the profit-harvest profile — NEW overrides, `disco` only; `pead` = the globals):**
```bash
DISCO_TAKE_PROFIT_PCT=10        # full-lot exit target for disco (vs pead's far TAKE_PROFIT_PCT=40).
                                #   The PRIMARY quick-win lever at current equity (see whole-share note).
DISCO_SCALE_OUT_TIERS=         # OPTIONAL tiered trim, e.g. "5:0.5" = sell 50% at +5%, ride the rest.
                                #   Default OFF at $2k equity (whole-share math defeats thirds — see below).
DISCO_TRAIL_ACTIVATE_PCT=      # OPTIONAL: lower than pead's 20 so the disco remainder trails sooner.
                                #   Default = inherit global (don't over-tune what we can't backtest).
```
`pead` lots read the existing globals verbatim. A disco knob left blank inherits the global, so the
change is strictly additive and reversible (clear the DISCO_* vars → identical to today).

## The whole-share reality check (do not skip)

At ~$2k equity with `MAX_POSITION_PCT=0.15` (~$310/name), disco lots are **1–7 shares** (today: CBRL 2,
ALOY 7, BKH 1…). Tiered scale-out by *thirds* is barely expressible: ⅓ of a 2-share lot rounds to 0 or
1 (a 0%/50% trim, not 33%). So:

- **At current equity, the quick-win lever is a single tighter full-exit TP (`DISCO_TAKE_PROFIT_PCT≈10`),
  NOT fractional scale-outs.** Cleaner, whole-share-native, and it still delivers "quick wins" + caps
  give-back. Recommend starting here.
- **Scale-out tiers are an equity-scales-up feature.** Spec the knob now; default it OFF until lots are
  large enough that a trim is ≥2 whole shares. Implementation rule when on: **trim whole shares only; a
  tier rounding to 0 shares is skipped; a lot too small to trim rides to TP/trail.** Never create a
  fractional remnant on purpose (fractional = synthetic-stop-only, no overnight protection — v2 plan §Other).

## Constraints this profile stresses

- **GFV / settled cash (cash account).** Quick-win = higher turnover = more proceeds stuck in T+1
  settlement and more Good-Faith-Violation surface if disco re-deploys fast. `CASH_SETTLEMENT_GUARD`
  already gates sizing on *settled* cash, so it fails safe (skips, doesn't violate), but disco may sit
  cash-starved more often. Measure it; this is the cost of churn in a cash account. (See the GFV note.)
- **Ledger attribution.** A tighter TP and any scale-out produce partial round-trips. `trade_ledger.py`
  FIFO already handles partial sells; the disco expectancy calc must treat multiple exits per entry as
  one round-trip's net. Verify before reading the verdict.
- **No honest backtest exists for disco.** `backtest_exit_policy.py` is the **pead** universe
  (mega-cap gap events). The disco universe is the small/mid discretionary tape our own research flagged
  as survivorship+recency biased. So these numbers (TP 10, tiers) are **reasoned starting points, not
  validated** — they MUST be earned forward in paper, not asserted.

## File-mapped changes

1. **`scripts/live_execute.py`** — add `book_caps(lot, caps) → dict` that overlays `DISCO_*` onto the
   base caps when `book_of(lot)=="disco"`. Route `trail_stop_price`, the TP check, and (if enabled)
   scale-out through `book_caps(lot, caps)` instead of the global `caps`. `pead` lots get the base dict
   unchanged.
2. **`scripts/apply_decision.py`** — mirror the same `book_caps` overlay in the paper exit path
   (keep-in-sync with live, per the existing parity note).
3. **`scripts/tick_context.py`** — resolve `DISCO_TAKE_PROFIT_PCT` / `DISCO_SCALE_OUT_TIERS` /
   `DISCO_TRAIL_ACTIVATE_PCT` into `caps` via `envf` (alongside the existing exit knobs at ~line 143).
4. **`.env` + `.env.example`** — the three knobs above, documented; `DISCO_TAKE_PROFIT_PCT=10` to start,
   tiers + trail-activate blank (OFF/inherit).
5. **`scripts/test_live_execute.py`** — unit tests: a disco lot exits at `DISCO_TAKE_PROFIT_PCT`; a pead
   lot ignores the disco knobs; the whole-share trim-rounding rule (tier→0 shares skipped).
6. **`scripts/pnl_report.py`** — the existing `--by-book` split already separates the verdicts; confirm
   partial-exit round-trips aggregate correctly per book.

## Validation & rollout (paper-first — no live money on a guess)

This is an **exit-profile** change, orthogonal to the v2 **capital-enforcement** Phase 1
(`BOOKS_ENABLED`). Books are already *tagged* (Phase 0 live), so per-book exits can be exercised with
`BOOKS_ENABLED=0`.

- **Step A — paper.** Ship behind paper (`run_paper_tick.sh` already routes books). Run ≥2 weeks /
  ≥30 disco round-trips. Read disco **expectancy net of costs** (`pnl_report.py --by-book`) and compare
  to the pre-change disco round-trips already in `trades.jsonl` (the honest baseline; note regime drift
  confounds an A/B in one account — flag it, don't hide it).
- **Step B — promote** to live only if disco-quick-win expectancy ≥ baseline disco net of costs **and**
  the GFV/cash-starvation cost (Step A measurement) is tolerable. Fold into the **2026-06-26** checkpoint
  that already reads both books.
- **Kill path unchanged:** disco's existing disarm (≥30 round-trips, expectancy<0, or P&L tripwire ×2)
  governs the whole book regardless of exit profile.

## Open questions for the owner

1. **TP level:** start `DISCO_TAKE_PROFIT_PCT` at **10**? (Quick, whole-share-clean. 8 = quicker/smaller,
   15 = closer to a swing.)
2. **Scale-out:** leave tiers OFF until equity grows (recommended), or force a single **50%@+5%** trim
   now to literally bank "a little along the way" despite the rounding coarseness?
3. **Disco trail:** leave the remainder on the global trail15@20, or trail the post-TP remnant sooner?
