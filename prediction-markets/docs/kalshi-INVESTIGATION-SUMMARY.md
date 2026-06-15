# Kalshi Prediction-Market Edge — Investigation Summary (2026-06-14)

> **STATUS — RESEARCH COMPLETE for this pass. No capital, no orders (Gate-3 rules hold).**
> Master index + verdict for the whole investigation: can an automated bot make money on Kalshi?
> Short answer: **no easy money — taker strategies are dead, and the maker edge, while real, is
> either inaccessible (HFT-only) or marginal ($3–43k/yr).** This doc links every sub-finding,
> script, and dataset so any human or agent can review or extend it.

---

## 1. Bottom line

| Strategy | Verdict | Evidence |
|---|---|---|
| Buy near-certain favorites (≥0.90) | **DEAD** −3.78%/bet | [favorite FINDINGS](kalshi-near-certain-favorite-FINDINGS.md) |
| Any taker trade (buy at the ask) | **DEAD** — no +EV bucket anywhere | [calibration FINDINGS](kalshi-calibration-FINDINGS.md) |
| Cross-venue arbitrage | **OFF** — geo-blocked from Polymarket + speed race | [research report](../../research/kalshi-bot-research.md) |
| Maker longshot-fade, **final-hours sports** | **REAL (+42% net, ~$22M/yr pool) but NOT FOR US** | [sports scope](kalshi-sports-mm-scope.md): pro capitalized MM only |
| Maker longshot-fade, **calm zone (6–24h, non-sport)** | **REAL but MARGINAL** (+21% net, ~$3–43k/yr) | [calibration FINDINGS](kalshi-calibration-FINDINGS.md) §Phase-A EV |

**There is no path here that is a business for a non-HFT solo operator.** The taker side is
dead three independent ways; the only +EV maker pocket we can actually reach tops out at
single-digit-to-low-tens of thousands of dollars a year behind a deep queue.

## 2. How we got there (the funnel)

1. **Multi-agent web research** ([research/kalshi-bot-research.md](../../research/kalshi-bot-research.md)) —
   adversarially-verified survey: fees, strategies, API, accounts, tooling, risks. Conclusion:
   the only edge with primary-research support is the *maker* side of the "optimism tax."
2. **Favorite probe** ([FINDINGS](kalshi-near-certain-favorite-FINDINGS.md)) — buy the ≥0.90
   favorite, hold to settlement. Measured **−3.78%/bet, t=−5.66**, robust to drop-top-N. Archived.
3. **Full taker calibration map** ([FINDINGS](kalshi-calibration-FINDINGS.md);
   `kalshi_calibration.py`) — every price bucket × category, ask-aware, net of fee. **Zero +EV
   buckets** survive the honest filter (|t|>2, non-degenerate price). Only robust signal: the
   longshot tail is a *reliable loser* to buy (the optimism tax).
4. **Real-fills validation at scale** (`kalshi_trades_calibration.py`) — 9.5M real trades from the
   open-source [TrevorJS/kalshi-trades](https://huggingface.co/datasets/TrevorJS/kalshi-trades).
   Takers lose **−6.06% on notional (−$53M)**; optimism tax huge & monotonic. Confirms no taker edge.
5. **Weather `[0.40–0.45)` OOS confirmation** — the one weak candidate **evaporated** (t 1.90→0.72).
   Taker now closed **three ways**.
6. **Forward L2 collection** (`kalshi_l2_collector.py`) — 2h, **346k snapshots**, 0 errors. Found
   cheap-longshot books **already stacked** (median 5,721 / mean 46,030 contracts resting on the ask).
7. **Phase-A maker EV model** (`kalshi_maker_ev.py`) — correct pre-fee accounting (see §3). The
   accessible calm-zone edge is real (+21% net) but tiny (~$803k/yr volume → ~$3–43k/yr realizable).

## 3. The key correction (important for reviewers)

A mid-investigation draft reported a **"+6% gross maker ceiling"** by setting
`maker_edge = −(taker_net)`. **That is wrong:** the taker's loss includes the **fee paid to
Kalshi** (~2.56% of premium overall; the `0.07·p·(1−p)` fee is smallest in absolute cents at the
extremes but, evaluated at a cheap price like p≈0.05, is ~6.6% *of premium* — largest as a % of a
low price). That fee goes to *Kalshi*, not the maker. The maker captures only the **pre-fee
transfer** `p − won − maker_fee`. Corrected figures:
- Overall maker edge: **+3.50% gross / +2.86% net** on notional (not +6%).
- The fee being huge on cheap contracts is *why* takers lose so much there — but most of that
  loss is rake, not a maker windfall. `kalshi_maker_ev.py` is the corrected accounting.

## 4. The maker edge, by zone (corrected, net of maker fee)

| zone | group | premium $ (1 shard) | maker NET edge | accessible? |
|---|---|---:|---:|---|
| <1h | SPORTS | 4.2M | +42.6% | no (HFT/fee-excluded) |
| 1–6h | SPORTS | 10.5M | +41.6% | no |
| 6–24h | SPORTS | 2.4M | −4.8% | n/a (no edge) |
| **6–24h** | **non-sport** | **0.23M** | **+21.4%** | **yes, but tiny** |
| 1–3d | non-sport | 0.35M | −19.3% | n/a |

Accessible-zone EV (calm, non-sport): ~$803k/yr taker premium × +21.4% net × realistic capture
(2–25% behind the queue) = **$3k–43k/yr**.

## 5. What we built (all reusable, public-data, read-only, stdlib/duckdb)

| script | purpose |
|---|---|
| `scripts/kalshi_pull.py` | favorite probe + liquid-series discovery cache |
| `scripts/kalshi_calibration.py` | full taker calibration map (candles); `--from`, `--category`, `--skip` |
| `scripts/kalshi_trades_calibration.py` | real-fills calibration on the 9.5M-trade dataset |
| `scripts/kalshi_maker_ev.py` | corrected maker EV model (pre-fee, by zone, queue-bounded) |
| `scripts/kalshi_l2_collector.py` | forward top-of-book L2 collector (unauth, public) |

Data (all **gitignored** under `data/`): `calibration.jsonl` + `calibration_report.txt`,
`calibration_weather_report.txt`, `trevorjs/joined_t0.parquet` (9.5M rows) +
`trades_calibration_report.txt` + `maker_ev_report.txt`, `l2/collect_20260614.jsonl` (346k snaps),
`kalshi_series_liquid.json` (discovery cache).

## 6. Docs index
- [research/kalshi-bot-research.md](../../research/kalshi-bot-research.md) — the web research report
- [kalshi-near-certain-favorite-FINDINGS.md](kalshi-near-certain-favorite-FINDINGS.md) — favorite probe (archived)
- [kalshi-calibration-FINDINGS.md](kalshi-calibration-FINDINGS.md) — taker map + real-fills + maker EV
- [kalshi-market-making-v1-plan.md](kalshi-market-making-v1-plan.md) — the maker pre-registered plan
- [kalshi-strategy-roadmap.md](kalshi-strategy-roadmap.md) — the decision tree
- [kalshi-sports-mm-scope.md](kalshi-sports-mm-scope.md) — scope of the big (sports) edge: real but not for us
- **this file** — the master summary

## 7. External data sources used / available
- **[TrevorJS/kalshi-trades](https://huggingface.co/datasets/TrevorJS/kalshi-trades)** (used) — trades + markets parquet, public.
- [Jon-Becker/prediction-market-analysis](https://github.com/Jon-Becker/prediction-market-analysis) — ~33GB, the 72M-trade study data.
- [thomaswmitch kalshi trades/markets](https://huggingface.co/datasets/thomaswmitch/kalshi-prediction-markets-trades), [analisto/kalshi_com](https://github.com/Ismat-Samadov/kalshi_com), [Kalshi tools-and-analysis](https://github.com/Kalshi/tools-and-analysis), [Kalshi Historical Data docs](https://docs.kalshi.com/getting_started/historical_data).
- **Note:** no open dataset has full L2 order-book *history* — that's why we collect it forward.

## 8. For a reviewer — where this could still be wrong
- **One trades shard (9.5M of ~160M).** Shards appear date-spanning (representative), but the EV
  uses a ×16 annualization — re-run across all 16 shards to tighten volume estimates.
- **EV capture fraction is assumed, not simulated.** We bound it 2–25% against observed queue
  depth; a true tick-level fill sim needs weeks of forward L2 through settlement (not built — the
  marginal ceiling didn't justify it).
- **L2 = 2h, top-of-book only.** Queue depth and spread are a 2-hour snapshot; longer collection
  and full-ladder depth would refine the queue model.
- **Sports/non-sports split is regex-on-ticker-prefix** — a few series may be misclassified.
- **`taker_side==result` win logic** assumes binary Yes/No settlement (true for these markets).
- Time-to-close uses `close_time − created_time`; for sports, `close_time` semantics (game start
  vs end) could shift the zone boundaries slightly.

---

**One-line bottom line:** Across web research, a favorite probe, a full taker calibration map, 9.5M
real fills, an OOS confirmation, 2h of live order books, and a corrected maker EV model — **Kalshi
offers no taker edge and only a marginal, queue-bound maker edge ($3–43k/yr accessible); the real
money (final-hours sports) needs HFT infrastructure we don't have. Recommended disposition: archive
as a thoroughly-documented "no durable retail edge," keep the tooling for any future hypothesis.**
