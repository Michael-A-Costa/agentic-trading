# Kalshi longshot-fade (maker) — FINDINGS (Phase A = REFUTED / ARCHIVED)

> **STATUS 2026-06-18 — REFUTED at n=61, Phase A ARCHIVED.** The maker / longshot-fade edge
> (roadmap Phase A — the last path with prior research support after favorite-buying and taker
> were killed) **loses money in the accessible zone.** Tested Gate-3-free (public top-of-book +
> public trades → adverse-selection backtest, no credentials, no capital). Run 1 (n=12) showed
> no accessible edge with the only positive an unreachable BTC fill-model artifact; the
> pre-registered **confirmation run 2 (accessible-only, n=61 settled weather/gas markets)
> settles it: net −$256 to −$897 across the whole stress bracket, t/market −0.37 to −0.88,
> drop-top-5 −$600 to −$1,400.** All three archive conditions tripped. **Decision: ARCHIVE.**

## Hypothesis (pre-registered)
Cheap longshots (yes-mid < 0.30) are overpriced (the "optimism tax"); a passive maker that
**rests a YES ask just above mid and holds to settlement** captures spread + the lower maker
fee against one-sided retail flow, and the longshot usually resolves NO. Falsifier: net ≤ 0
after queue position, adverse selection, latency, and fees — or significance carried by a few
markets (fails drop-top-N).

## Method
- **Pipeline** (`scripts/`, stdlib, built+tested 2026-06-16): `kalshi_public_tape.py` collects a
  forward, unauth, harness-shaped tape (top-of-book snapshots + every public trade, longshot band
  only); `kalshi_maker_replay.py` (v1, 16-assert selftest) scores it with the honest fill rule —
  **a resting quote fills only when flow crosses it AND after the queue ahead at our price is
  consumed.** Fair value = yes-mid; policy = fade @ mid+1¢, size 100, event-driven re-quote.
- **Stress bracket**: queue {optimistic | pessimistic (re-pad to live displayed size)} ×
  latency {0, 250, 500 ms cancel-pickoff} × maker-fee {0, 0.07 coef ceil(C·P·(1−P))}.
- **Stats on the honest unit** — per-MARKET round-trips (per-fill t-stats are shown only as
  `_NAIVE` to expose their inflation), plus **drop-top-N markets** (the repo's survivorship gate).
- **Run 1**: 2026-06-16, 6 series (5 weather/gas + daily BTC), ~2h, **214 markets / 6,231 real
  trades / 205 settled (203 NO, 2 YES)**.

## Result — run 1

**Accessible zone (weather/gas — what we could actually trade), n = 12 markets:**
| Config | Net | t / market | drop-top-5 |
|---|---|---|---|
| optimistic / 0ms / fee 0 (best case) | **+$31** | 0.27 | **−$66** |
| pessimistic / 250ms / fee 0 (realistic) | **−$9** | −0.09 | **−$73** |
| pessimistic / 500ms / fee 0.07 (worst) | **−$108** | −0.61 | **−$157** |

Net ≈ zero at best, negative under realistic assumptions, **t indistinguishable from zero
(|t| ≤ 0.3)**, and **drop-top-5 negative in every config.** No edge.

**The headline "+$881" is not accessible edge.** ~99% is `KXBTCD` (daily Bitcoin near-money
strikes): $890 of the $881 total, and **84% from just 2 markets** (`T66099` +$548, `T66199`
+$190). BTC-only is +$890 but t=1.6 with drop-top-5 collapsing to +$70 — and it's the
HFT-contested zone the roadmap flags as inaccessible. Our L1 "pessimistic" queue still credits us
**3,000+ passive near-money fills** we would never win against Jump/SIG ⇒ a **fill-model
artifact**, not a tradable edge.

## Interpretation
1. **No edge where we can trade.** In the accessible calm zone the sign is ~0 and turns negative
   once realistic queue/latency/fees apply. The longshot tail *did* resolve as assumed (203/205
   NO) — but we barely get filled resting passively, and when we do fill near settlement it's
   adverse (one −$87 KXHIGHLAX market on a YES upset). That is the adverse-selection story the
   roadmap warned was the whole risk.
2. **The "edge" is artifact + unreachable + concentrated** — three independent disqualifiers on
   the BTC number: a too-generous L1 fill model, an HFT zone we can't access, and 2-market
   concentration that fails drop-top-N.
3. **Consistent with every prior result here**: favorite-buying dead (−3.78%/bet, t=−5.66),
   taker dead (9.5M-fill confirm), and now maker longshot-fade = no accessible edge — exactly the
   roadmap's honest prior ("thin, capacity-bound, not a business for a non-HFT solo operator").

## Result — run 2 (confirmation, accessible-only, n = 61 settled weather/gas markets)
2026-06-17 collection, 20 daily weather/gas series (BTC/crypto/sports excluded), 263 rounds,
209 markets / 30,547 trades; 61 settled at score time.

| Config | Net | net/contract | t / market | drop-top-5 |
|---|---|---|---|---|
| optimistic / 0ms / fee 0 | **−$759** | −0.043 | −0.69 | −$1,345 |
| optimistic / 0ms / fee 0.07 | −$861 | −0.049 | −0.78 | −$1,412 |
| pessimistic / 250ms / fee 0 (realistic) | **−$811** | −0.053 | −0.80 | −$1,274 |
| pessimistic / 250ms / fee 0.07 | −$897 | −0.059 | −0.88 | −$1,332 |
| pessimistic / 500ms / fee 0.07 | −$256 | −0.018 | −0.37 | −$606 |

**Negative in every config; t/market negative (not merely ≈0); drop-top-N deeply negative.**
Run 1's small-sample +$31 best-case evaporated at 5× the markets. By series the result is
two-sided — some cities net positive (LAX +$247, MIA +$172) — but the losers dominate
(KXHIGHNY −$576, KXHIGHAUS −$541, KXHIGHDEN −$332). The big losses are **adverse settlements**:
buckets where the temperature actually landed in-the-money (the longshot hit), real buying flow
crossed our resting YES ask, and we were short into a YES resolution at ~−$0.80/contract. That is
"pennies in front of a steamroller," measured — the exact adverse-selection risk the roadmap
named, and it is net-losing.

## Verdict — Phase A ARCHIVED (pre-registered rule, all three conditions tripped)
Frozen rule was: net ≤0 realistic **or** drop-top-5 ≤0 **or** t/market < 2 → refute + archive.
At n=61 the realistic config is **net −$811, drop-top-5 −$1,274, t/market −0.80** — every
condition fails decisively, with the sign *negative*, not marginal. **Longshot-fade is dead;
Phase A is archived.** Tooling (`kalshi_public_tape.py`, `kalshi_maker_replay.py`,
`kalshi_l2_ws_collector.py`) is retained for any future prediction-market hypothesis. Full L2
depth (the parked Gate-3 WS collector) would only *tighten* the fill model and make this worse,
not rescue it. No band/spread sweep is warranted: 203/205 longshots resolved NO yet the trade
still loses, because the limiter is adverse fills on the few that hit, not the resolution edge.

## Status of the rest
Pipeline + harness retained and reusable. The Gate-3 WS collector (`kalshi_l2_ws_collector.py`)
stays parked — full L2 depth would *tighten* the fill model (and only make the accessible number
worse, since the BTC artifact shrinks with realistic depth), not rescue the edge. See
[`cloddsbot-collector-review.md`](cloddsbot-collector-review.md) and
[`kalshi-strategy-roadmap.md`](kalshi-strategy-roadmap.md).
