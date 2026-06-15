# Final-Hours Sports Market-Making — Scope & Feasibility (2026-06-14)

> **STATUS — SCOPING NOTE. No capital, no orders (Gate-3 rules hold).** Sizes the one big maker
> edge the EV model found — fading cheap-YES sports longshots in the final hours — to decide if
> it's worth an HFT/automated-MM build. Data: `scripts/kalshi_sports_scope.py` on the 9.5M-trade
> sample (`data/trevorjs/sports_scope_report.txt`). Verdict up front: **a large, real edge, but
> not viable for this operation; pursuable only by a well-capitalized, infra-serious automated MM.**

## 1. The prize (TAM)
- **~$52M/yr** of taker premium flows into final-6h sports cheap-YES longshots.
- **~$22M/yr total maker net pool** (the transfer from those takers to *all* makers).
- Longshot **hit rate ~3.5%** — the maker wins ~96.5% of fills (keeps the premium), pays $1 on the rest.
- **NFL / NCAA dominate:** NFL games ~$6.8M/yr maker pool (hit rate ~0%!), NCAAF ~$5.9M, NCAAMB ~$4.7M, MVE sports ~$2.8M.

This is a genuinely large pool — orders of magnitude above the accessible calm-zone (~$0.8M/yr).

## 2. How fast is the edge, really?
Correcting my own "HFT-only" shorthand — the edge is **not** purely microsecond:

| window to close | maker net edge | premium $/shard | hit % |
|---|---:|---:|---:|
| <15 min | +75.6% | 0.7M | 0.8% |
| 15–60 min | +36.1% | 3.5M | 3.6% |
| **1–3h** | **+41.4%** | **6.6M** | 4.1% |
| 3–6h | +41.9% | 3.9M | 3.4% |
| 6h+ | +3.6% (collapses) | 3.0M | 4.9% |

The bulk of the edge **and** volume sits in the **1–6h pre-game window** at ~+41% net — that's
*fast automated* market-making (sub-minute cancel on news/price moves), **not** necessarily
ultra-low-latency HFT. The <15min window is richer (+76%) but tiny and is the fast-money zone.

## 3. The real barriers (why it's still hard)
Latency is *not* the main wall. These are:
1. **Queue / incumbents.** The cheap-longshot ask is already stacked (L2: median 5,721 / mean
   46,030 contracts resting) with Jump/SIG-class makers. You join at the back and capture a slice.
2. **The marginal edge < the average.** The +41% is *realized over fills incumbents took*. A new
   entrant gets the flow they *didn't* want — i.e., worse adverse selection — so your realized
   edge would be below +41%, possibly well below.
3. **Capital is gated by margin, not premium.** Selling a cheap longshot collects ~5¢ but Kalshi
   margins the **$1 max-loss** exposure. Capturing a meaningful slice of a $22M pool means resting
   large size = large collateral. Premium ≠ capital required.
4. **Short-vol tail risk.** Each hit pays ~20× the premium collected ($7.4M paid on hits vs $13.8M
   kept on wins per shard). Selling cheap longshots is shorting a basket of lottery tickets;
   **correlated upsets (a chalk-heavy slate all hitting) are the blow-up mode.** Hard inventory/
   per-event/global caps and a kill switch are mandatory.
5. **Fee-rebate excluded.** Sports is excluded from Kalshi's volume-rebate program — no kicker.

## 4. Feasibility verdict
**For this operation (agentic-trading: small funding, Python-loop, solo): NOT viable.** You'd be
under-capitalized for the margin, behind the incumbent queue, and exposed to the short-vol tail
with no edge cushion. You'd be the slow/under-capitalized money the pool is *paid by*, not paid.

**For a serious, well-capitalized automated-MM build: plausible but a major undertaking.** It would
require: real-time sports data feeds (to price/cancel on news), a fast event-driven quoting engine
(sub-minute cancel), meaningful margin capital, robust short-vol risk controls, and a credible plan
to win queue priority against Jump/SIG. That is a months-long, capital-intensive project competing
with professional firms — not a research-subtree experiment.

## 5. If it were ever pursued (the build sketch)
1. Forward-collect full L2 *through settlement* on the top sports series (NFL/NCAA) for several
   weeks — to model queue position and fill probability (not just top-of-book).
2. Build a fair-value model per market from a real-time sports feed; quote a fade offer with a
   dynamic edge buffer; **cancel on adverse moves within seconds.**
3. Backtest queue-and-cancel-aware fills on the collected L2; prove the *marginal* (not average)
   edge survives.
4. Only then, Gate-3: separate repo, hard margin/inventory/daily-loss caps, kill switch, small size.

## 6. Bottom line
The sports edge is **real and large (~$22M/yr pool, +41% net in the 1–6h window)** — but it's a
**professional, well-capitalized automated-MM business**, gated by queue position, margin capital,
and short-vol tail risk, not by a clever script. **Recommended: do not pursue from this operation.**
It stays in the "documented, understood, not for us" column alongside the rest of the Kalshi work.

*Artifacts:* `scripts/kalshi_sports_scope.py`, `data/trevorjs/sports_scope_report.txt`. See the
[investigation summary](kalshi-INVESTIGATION-SUMMARY.md) for the full picture.
