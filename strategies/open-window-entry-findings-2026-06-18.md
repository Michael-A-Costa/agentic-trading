# Open-window entry discipline — findings & forward checkpoint (2026-06-18)

**Status:** ARMED live 2026-06-18 (paper mirrors). Adjudication deferred to **≥30 open-window
round-trips** (see *Forward checkpoint*). No further dial changes until then.

## What & why

Trigger: 16 buys (~$2,500) fired in the first 12 minutes of 2026-06-18 — almost all low-conviction
disco gap-down dip-buys. The open-sweep drains the whole morning slate at once and each DD COMMIT
force-triggers a tick, so the per-tick `MAX_ENTRIES_PER_TICK=5` never bounds the *window* total.

Two replays over the 97 broker-truth round-trips (`scripts/entry_timing_replay.py`,
`scripts/open_select_backtest.py`) agree on the headline:

> **The first 60 minutes is the loss centre.** Take-all over the 31 in-window round-trips =
> **−$42.26, PF 0.28, 29% win**. Everything net-positive sits *after* the first hour (60m+ = +$51, PF 1.96).

Cross-tab (realized $ / n): the toxic cell is **early AND extended** — first-60m × ext≥10% = **−$35/18**.
ext≥10% is *fine* after the hour (+$28); the 3–10% extension band earns PF 1.6–1.9.

## Shipped config (the controls)

`decide.py` open-window selector + `tick_context.py` extension gate, all env-tunable:

| Knob | Value | Backing |
|---|---|---|
| `OPEN_GATE_MAX_EXT_PCT` | **10** (was 6) | 6 wrongly clipped the profitable 6–10% band; 10 is the replay's line |
| `OPEN_GATE_MODE` | **enforce** (was shadow) | drops in-window entries ≥10% extended from the open |
| `OPEN_SELECT_MIN_CONVICTION` | **medium** (pead exempt) | see floor justification below |
| `OPEN_SELECT_MAX_ENTRIES` | **3** per window | safety ceiling; rarely binds historically, binds hard on flood days |
| `OPEN_SELECT_WINDOW_MIN` | **60** | the loss window |

Selector logic in-window: (1) extension gate, (2) conviction floor (pead-qualified exempt),
(3) survivors **compete** on a score (conviction → pead → 3–10% extension sweet-spot → closing
strength) for ≤`cap` slots, tracked across the open-sweep's force-ticks via `trades.jsonl`. Benched /
below-bar names re-DD and may still enter **after** the window, where the edge actually is.

### Config backtest (`open_select_backtest.py`, 31 in-window round-trips, today excluded — still open)

| gate | floor | cap | keptN | kept$ | keptPF | dropN | drop$ |
|---|---|---|---|---|---|---|---|
| take-all | — | ∞ | 31 | **−42.26** | 0.28 | 0 | 0 |
| 10 | low | 2 | 9 | −3.29 | 0.80 | 22 | −38.97 |
| **10** | **medium** | **3** | **3** | **+6.04** | **2.21** | 28 | −48.30 |
| 10 | high | 2 | 2 | +11.02 | ∞ | 29 | −53.28 |

Chosen **gate10 / medium / cap3**: +$6.04 kept (PF 2.2) while shedding a −$48 losing tail. `high`
scores higher but keeps 2 of 31 trips — too thin to trust and would near-silence the open. The raw
argmax (`high/cap2`) is overfit (PF "∞" = 2 trips, zero losers) and was **not** adopted.

**Floor caveat resolved:** the `medium` floor looked to contradict the 6/15 finding that low-conviction
disco is a profit centre (entry-gate hypothesis refuted). The backtest shows both are true — low pays
**all-day** but **loses in the first 60 min** (`gate10+low` negative; `gate10+medium` positive). The
floor is justified *because it is scoped to the open window*, which the all-day 6/15 study didn't isolate.

## Live counter-example — 2026-06-18 (HONEST, n=1, unrealized)

The shipped config replayed against today's actual open: **16 buys → 3 kept** (USAR, RARE, UUUU;
KMX & CLPT pead-passed the floor but lost the cap; the other 10 dropped on floor, FRMI on the gate).

As of **10:37 ET** (positions still open, ~1h in):

| disposition | name | conv | P&L (mark) |
|---|---|---|---|
| **KEEP** | USAR | medium | −$3.41 |
| **KEEP** | RARE | medium | −$3.33 |
| **KEEP** | UUUU | medium+pead | +$0.09 |
| | **kept-3 total** | | **−$6.65** |
| bench (cap) | CLPT | low+pead | **+$7.24** |
| bench (cap) | KMX | low+pead | +$1.38 |
| drop (gate) | FRMI | low | +$2.48 |
| drop (floor) | WOLF | low | **+$9.84** |
| drop (floor) | PZZA | low | +$2.56 |

**The config kept the three weakest and benched the day's biggest winner (WOLF +$9.84) plus a +$7.24
name (CLPT).** This is the eyeball-one-day trap inverted against us: a single open session disagreeing
with a 31-trip settled prior. It is **unrealized** (swings still open; WOLF can give it back, USAR can
recover) and it is **not** a tripwire — but it is logged so the forward checkpoint judges on data, not
on the narrative that "the gate clearly works."

## `range_pos` was dead on live — fixed 2026-06-18 (forward-only)

While auditing the selector score, found its closing-strength term `(range_pos or 0)*10` was contributing
**exactly 0 on live**. `range_position()` needs day high/low; `rh_direct` (the live quote primary) returns
only last/prev_close/bid/ask — **no OHLC**. So `range_pos` has been `None` on every live candidate since
~2026-06-10 (24,828 null vs 779 non-null, the latter all 6/08–6/10 pre-rh_direct). `intraday_pct` survived
only via its prev-close fallback; `range_position` had none.

**Fix:** `mc.backfill_ohlc()` fills missing open/high/low from keyless Cboe for held+candidate symbols
(not indexes), called in `tick_context` after the fetch, before the quote cache is persisted — without
touching the rh_direct last/bid/ask (owner data-rule: rh_direct=price, keyless=structure). `range_position`
now also extends the range to include `last`, so a real-time last above a 15-min-delayed Cboe high reads
1.0 instead of >1. Gated by `QUOTES_OHLC_BACKFILL` (default on); fail-soft (range_pos stays None on Cboe error).

**Forward-only — the backtest cannot recover this.** Historical range_pos was never logged (null), and it's
not cleanly reconstructable: it needs the *intraday* high/low at each fill instant, but Cboe history is
daily bars (= lookahead). So `open_select_backtest.py` still shows range_pos contributing 0 over the 31
settled trips. range_pos's value becomes a **checkpoint question**: at ≥30 in-window round-trips placed
with the term live, does closing-strength separate winners (a WOLF at range_pos→1.0) from faders (a SIMO)?
Today's ~11:00 snapshot is suggestive — winners WOLF/CLPT at 1.0 vs kept USAR 0.62 / RARE 0.41 / UUUU 0.21 —
but that's the late mark, not the value at the 09:34 fill, so it is **not** evidence, only motivation.

## Forward checkpoint (pre-registered — do not loosen after looking)

Re-run `entry_timing_replay.py` + `open_select_backtest.py` once the ledger holds **≥30 open-window
round-trips placed under the armed config**. Decision rule:

1. **Confirm** if kept-set expectancy is net-positive AND the deferred/dropped set is net-negative
   (we're shedding losers, not winners) → keep gate10 / medium / cap3.
2. **Tighten** toward `high` floor / `cap2` only if the kept set is still net-negative AND high-conviction
   in-window is the only positive cohort with n≥10.
3. **Loosen** (`OPEN_SELECT_MIN_CONVICTION=low`, floor off; keep gate + cap) if the medium floor's
   deferred set is net-**positive** over the window — i.e. it is benching winners systematically, not
   just on 6/18. This is the live counter-example's escape hatch.
4. **Disarm** (`OPEN_SELECT_MAX_ENTRIES=-1`, `OPEN_GATE_MODE=shadow`) if the armed-config open-window
   expectancy is no better than take-all net of costs.

Track the deferred names' *forward* outcome too: a deferred name that re-enters post-window and pays
is the system working; a deferred name that would have paid only at the open is evidence for #3.

## Re-run

```
python3 scripts/entry_timing_replay.py        # bucketed timing/extension/anchor evidence
python3 scripts/open_select_backtest.py        # config sweep over in-window round-trips
```
