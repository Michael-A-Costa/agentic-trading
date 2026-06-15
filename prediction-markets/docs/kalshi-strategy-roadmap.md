# Kalshi — Money-Making Strategy Roadmap (EXPLORATORY; the decision tree)

> **STATUS 2026-06-14 — RESEARCH ROADMAP. Research-only until Gate 3** (no capital, no orders,
> no wallet — `CLAUDE.md`). This is the sequencing layer that ties together the favorite
> FINDINGS (dead), the market-making plan, and the new taker-calibration backtest, and decides
> *what we actually build to try to make money.* The owner has lifted the prior speed
> constraint — **faster execution / a real quoting loop is now on the table** — which reopens
> the one edge with primary-research support. Source of facts: `research/kalshi-bot-research.md`.

## 0. The honest map (where money can and can't be)

The research + our own measured run leave a small, sharp menu. The scissors that used to kill
everything (the evidence-backed edge needs speed we lacked) is **half-removed** now that we can
build a faster loop — so the maker path reopens. But two hard facts still bound us:

- **Fees set a high floor.** A mid-book taker round-trip needs ~3.5% of edge before spread; only
  **maker positions held to settlement** (near-zero fee) clear it comfortably.
- **Kalshi serves no historical L2 order-book.** Only 1/60/1440-min candles + a bids-only
  snapshot. ⇒ *Taker* statistical edges are backtestable on history **now**; *maker* edges
  (spread capture, adverse selection) require **forward L2 collection first** — a build, not a
  backtest.

| Strategy | Backtestable now? | Status |
|---|---|---|
| Favorite-buying (≥0.90) | done | **DEAD** — measured −3.78%/bet, t=−5.66 ([FINDINGS](kalshi-near-certain-favorite-FINDINGS.md)) |
| **Taker mispricing — full calibration curve** | **yes, running** | **Phase 0** — decides whether any taker pocket exists at all |
| **Passive maker / longshot-fade** | no (needs L2 collector) | **Phase A** — the evidence-backed edge; speed now unlocked |
| **Post-news drift / base-rate divergence** | partly (needs event tape) | **Phase B** — fits our infra + equity DNA; weak profit evidence |
| Cross-venue arb | n/a | **OFF** — owner geo-blocked from Polymarket; speed race anyway |
| Paid DLP program | n/a | **LATER** — signed Market Maker Agreement; only after a track record |

## Phase 0 — Taker calibration map *(running 2026-06-14)*

**Tool:** `scripts/kalshi_calibration.py` (generalizes `kalshi_pull.py`). For every settled liquid
market at 24h-before-close it prices **both** taker trades at the ask (buy YES @ yes_ask, buy NO @
1−yes_bid), nets the published fee, and bins by **price paid** across the whole [0,1] curve, by
category. Output → `data/calibration.jsonl` + `data/calibration_report.txt`.

**Pre-registered decision:**
- **Any bucket/category +EV net of fee (n≥30, surviving drop-top-5)** ⇒ a candidate **taker** edge
  — verify at larger n and live per-series fee, then it's directly tradable (no maker infra needed).
- **No +EV pocket (the expected outcome)** ⇒ the curve is efficient/rich; *taker is dead full-stop*
  and the only path is **Phase A (maker)**. This is the decisive gate that tells us whether to
  invest in the L2 collector.

> **Phase 0 verdict (2026-06-14): NO taker edge — confirmed dead.** Swept top-40 liquid series,
> 3,608 settled markets → 2,460 taker trade-rows, 24h horizon (`data/calibration_report.txt`,
> rows in `data/calibration.jsonl`). With an honest filter (|t|>2, drop-top-5 robust, price in
> (0.05,0.95)) **zero buckets are +EV net of fee.** The only statistically robust signal is
> *negative*: the longshot tail `[0.00–0.30)` loses −50% to −85% (t up to −6.6) — the "optimism
> tax" confirmed. Apparent positives were artifacts: the `[0.40–0.45)` weather bucket (+31%) is
> t=1.9, below significance and a lone bucket among ~140 tested (≈7 false positives expected by
> chance); the high-t `[0.95–1.00)` commodities/crypto buckets are degenerate near-certain pennies
> (variance≈0), i.e. the favorite-buy trade FINDINGS already killed.
>
> **Decision: taker is dead full-stop. The only path with a positive-EV implication is the MAKER
> side of that same longshot tail (sell/fade the overpriced longshot) → Phase A, which needs the
> forward L2 collector.**
>
> **Confirmed three ways (2026-06-14):** (1) this candle map; (2) **9.5M real fills** from the
> open-source TrevorJS dataset — takers lose −6.06% on notional, −$53M, with the optimism tax
> huge and monotonic on longshots (see [calibration FINDINGS](kalshi-calibration-FINDINGS.md) +
> `scripts/kalshi_trades_calibration.py`); (3) the weather `[0.40–0.45)` candidate **evaporated**
> on an expanded OOS sample (t 1.90 → 0.72). The Phase-A maker EV model (`scripts/kalshi_maker_ev.py`)
> then bounded the maker side with CORRECT pre-fee accounting (takers' fee goes to Kalshi, not the
> maker): real maker edge is **+2.86% net overall**, concentrated in **inaccessible final-hours
> sports** (+42% net, HFT-competitive, fee-excluded). The accessible calm zone (6–24h, non-sports)
> is **+21% net but only ~$803k/yr of volume behind a deep queue → ~$3–43k/yr realizable.**
> **Verdict: a real but marginal edge — not a business for a non-HFT solo operator.**

## Phase A — Passive maker / longshot-fade *(the evidence-backed path; needs a build)*

The one edge with primary-research support: makers earn the spread + the lower maker fee against
one-sided retail flow (the "optimism tax"). Cleanest expression = **make the YES side / take NO on
cheap longshots** (the longshot is overpriced; rest liquidity that retail crosses). Full hypotheses,
falsifiers, and gates: [`kalshi-market-making-v1-plan.md`](kalshi-market-making-v1-plan.md).

**The whole risk is adverse selection** — you fill when fair value just moved against you — and the
**only defense is a real quoting loop**: re-quote and cancel fast as the book moves. *This is where
the unlocked speed actually buys something.* Build order (demo-only until Gate 3):

1. **WebSocket L2 collector** (snapshot + `orderbook_delta`, bids-only reciprocity, error-code-25
   handling) → record the book + fill tape for the target series, **forward, for N weeks.** This IS
   the dataset Phase A's backtest needs; nothing downstream can start without it.
2. **Faster tick** — a maker needs sub-second-ish re-quote/cancel on book moves, not the ~4-min
   equity loop. Design the quoting loop around the WebSocket delta stream (event-driven, not polled).
3. **Adverse-selection-aware backtest** on the collected data: fill only when fair value reaches/
   crosses the resting quote; model queue position; net live fees; **drop-top-N**. Pass = positive
   net of all of that.

**Honest ceiling:** edge is thin, decaying (ψ shrinking since pro-MM entry Oct-2024), and
capacity-limited on the tail. Speed makes it *survivable*, not *large*. We are not out-HFT-ing Jump
/ Susquehanna in flagship markets — the shot is the **thinner, less-contested series** they ignore.

## Phase B — Post-news drift / base-rate divergence *(fits our infra + DNA)*

A **low-frequency statistical** edge — the direct port of our equity drift/PEAD work — that the slow
loop handles fine and that doesn't require winning the spread as a maker. After a discrete catalyst
moves a market's fair probability, does the contract **underreact and drift** over H hours/days?
(H1 in the Polymarket note.) Backtestable on candles **once we align an event/timestamp tape** to the
price series (the missing piece — a dated headline/print log per underlying).

**The catch (be honest):** weakest profit evidence in the report. Miscalibration is well-documented
but lives in the highest-cost zone (widest spreads), and you can't out-forecast macro markets. Cheap
and on-thesis to test, **low expectation.** Run it because it's nearly free and reuses our harnesses,
not because we expect it to print.

## Sequencing & gates

1. **Phase 0 (now)** — read the calibration map. If a taker pocket exists, chase it first (no build).
2. **Phase A build (if Phase 0 is dead, or to harvest spread regardless)** — stand up the L2
   collector + faster quoting loop; collect forward; then the adverse-selection-aware backtest.
   Gate exactly as `kalshi-market-making-v1-plan.md` §5.
3. **Phase B (parallel, cheap)** — assemble an event tape; run the drift probe; low expectation.
4. **Gate 3** before any capital: separate repo, hard caps, kill switch, price floor (never quote
   < ~$0.15), self-throttle (429 has no `Retry-After`), CPA on tax classification.

**Default outcome if the gates fail: archive.** The report's honest read is that durable retail edge
here is thin — Kalshi may be a discipline-sharpening exercise more than a P&L machine. We build the
collector and run the tests precisely to find out with our own data, not to assume.

---

**One-line bottom line:** Favorite-buying is dead; Phase 0 (running) tells us if *any* taker pocket
exists; if not, the only real shot is **Phase A — a faster maker/longshot-fade loop on the thin
contested-less series**, which the now-unlocked speed makes survivable (not large); Phase B (news
drift) is the cheap, on-DNA long shot. Everything stays research-only until it clears the gates.
