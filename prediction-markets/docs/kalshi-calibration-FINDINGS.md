# Kalshi Taker Calibration Map — FINDINGS (no taker edge)

> **STATUS 2026-06-14 — ARCHIVED NO-EDGE RESULT (taker side closed; confirmed 3 ways).**
> Research artifact, no capital. Tests whether *any* taker strategy (buy at the ask) is +EV
> net of fees anywhere on Kalshi — generalizing the favorite probe
> ([kalshi-near-certain-favorite-FINDINGS.md](kalshi-near-certain-favorite-FINDINGS.md)) to the
> whole price curve × category. **Verdict: no taker-harvestable pocket.** The only robust signal
> is *negative* (the longshot "optimism tax"), which points to the **maker** side
> ([kalshi-market-making-v1-plan.md](kalshi-market-making-v1-plan.md)). This doc is written to be
> independently reviewable — see §Reproduce and §For a reviewer.

## Question
The favorite band (≥0.90) was already measured dead (−3.78%/bet). Open question: is the *rest* of
the curve efficient/rich, or is there some price region / category where buying at the ask beats
fees? If a +EV pocket exists, it's directly tradable with no maker infra. If not, only the maker
path (which needs forward L2 collection) remains.

## Method
- **Tool:** `scripts/kalshi_calibration.py` (generalizes `kalshi_pull.py`; public, read-only REST).
- **Universe:** top-40 liquid recurring short-dated series by open interest (from the
  `kalshi_pull.py --discover` cache). The live sweep scanned 3,608 settled markets (parlays
  excluded); 1,374 had usable candle snapshots that qualified. *(The saved `--from` re-report's
  "scanned" line shows the 1,374 distinct qualifying markets, not the original 3,608.)*
- For each settled market at **24h before close**, reconstruct the snapshot from candlesticks and
  price **both** taker trades you could actually put on **at the ask** (buy YES @ `yes_ask`, buy
  NO @ `1 − yes_bid`) — real buy cost, **not mid**. Record realized outcome + net P&L net of the
  published fee `0.07·p·(1−p)`. **Bin by the price you pay** across the whole [0,1] curve, by
  category. → 2,460 taker trade-rows.
- **Discipline (same as the rest of the subtree):** ask-aware fills (never mid), net of fee,
  **drop-top-5-winners** stress test, naive t flagged optimistic (resolutions cluster ⇒ not iid).
- **Honest lead filter:** a bucket is a candidate edge only if net_ret>0 **and** n≥30 **and**
  drop-top-5 robust **and** **|t|>2** **and** price ∈ (0.05, 0.95) (excludes degenerate
  near-certain "pennies" whose high t is just ~zero variance).

## Result (ALL CATEGORIES, n = 2,460; full table in `data/calibration_report.txt`)
| price bucket | n | mean_px | realized win | calib (win−px) | net_ret | drop-top-5 | t |
|---|---:|---:|---:|---:|---:|---:|---:|
| [0.00–0.05) | 504 | 0.016 | 0.006 | −0.011 | **−85.4%** | −106.9% | −6.62 |
| [0.05–0.10) | 179 | 0.068 | 0.034 | −0.035 | **−60.0%** | −100.1% | −3.13 |
| [0.10–0.15) | 104 | 0.119 | 0.067 | −0.052 | **−51.8%** | −91.7% | −2.57 |
| [0.25–0.30) | 80 | 0.273 | 0.263 | −0.010 | −8.3% | −28.3% | −0.45 |
| [0.40–0.45) | 73 | 0.421 | 0.534 | +0.114 | +23.0% | +14.0% | 1.64 |
| [0.45–0.50) | 82 | 0.471 | 0.451 | −0.020 | −8.4% | −16.6% | −0.72 |
| [0.50–0.55) | 86 | 0.520 | 0.535 | +0.015 | −0.4% | −6.4% | −0.04 |
| [0.60–0.65) | 76 | 0.621 | 0.526 | −0.095 | −18.0% | −23.8% | −1.94 |
| [0.90–0.95) | 133 | 0.924 | 0.955 | +0.031 | +2.8% | +2.5% | 1.44 |
| [0.95–1.00) | 353 | 0.977 | 0.963 | −0.014 | −1.6% | −1.7% | −1.51 |

**Leads surviving the honest filter (|t|>2, non-degenerate price): ZERO.**

## Interpretation
1. **No taker edge.** Every bucket that clears the honest filter does so on the *wrong* side
   (negative). The curve is efficient-to-rich: you pay the fee + the spread, so net_ret hugs
   slightly-negative wherever the market is well-calibrated.
2. **The one robust signal is the longshot "optimism tax."** The cheap-YES tail `[0.00–0.30)` is
   significantly −EV to *buy* (−50% to −85%, t down to −6.6) — retail overpays for longshots.
   This is the Becker/GWU result reproduced on our own pull. **Its only +EV implication is the
   MAKER side: sell/fade the overpriced longshot** — untestable on historical candles, needs
   forward L2 (Phase A).
3. **The apparent positives are artifacts** (do not trade them):
   - `[0.40–0.45)` +23% (weather +31%) — **t=1.64/1.90, below significance**, a lone bucket among
     ~140 tested (≈7 false positives expected by chance at t≈1.6). *Confirmation sweep running —
     see §Pending.*
   - Commodities/Crypto `[0.95–1.00)` showed t=13.9/9.4 in the raw run — a **trap**: near-certain
     contracts where every sampled market won, so variance≈0 inflates t. It's the favorite-buy
     trade FINDINGS already killed; excluded by the price∈(0.05,0.95) filter.

## Decision
**Taker is dead.** Do not build a taker strategy. The only path with a +EV implication is the
**maker** side of the longshot tail → [kalshi-market-making-v1-plan.md](kalshi-market-making-v1-plan.md)
Phase A, which is why the forward L2 collector is now running (§Pending).

## Real-fills validation at scale (2026-06-14) — 9.5M trades

The candle map above rests on ~2,460 *synthetic* ask-aware trades. We re-ran the same question
on **REAL executed trades** from the open-source **[TrevorJS/kalshi-trades](https://huggingface.co/datasets/TrevorJS/kalshi-trades)**
dataset (HuggingFace, public, MIT-adjacent; ~160M trades across 16 shards). Using one 10M-trade
shard joined to market outcomes → **9.5M settled trades, ~2.0B contracts, $877M notional**.
Tool: `scripts/kalshi_trades_calibration.py` (duckdb); each trade priced at what the taker
actually paid, won iff `taker_side == result`, net of the `0.07·p·(1−p)` fee.

**Headline (the wealth transfer) — CORRECTED accounting:**
- Takers lose **−6.06% on notional = −$53.1M** (this shard). But the taker's loss is NOT all the
  maker's gain: **~2.56% of premium is the fee takers pay to KALSHI**, not to the maker.
- The maker captures only the **pre-fee** transfer: **+3.50% GROSS / +2.86% NET on notional**
  (after the 0.0175 maker fee). *(An earlier draft said "+6% gross" by mistakenly setting
  maker_edge = −(taker_net), double-counting Kalshi's fee — corrected here and in
  `scripts/kalshi_maker_ev.py`.)*

**The optimism tax is huge and monotonic** (taker realized return on stake, by price paid):

| price band | n trades | taker net | read |
|---|---:|---:|---|
| [0.00–0.05) | 547k | **−72.0%** | retail massively overpays for cheap longshots |
| [0.05–0.10) | 457k | −56.9% | |
| [0.10–0.25) | ~1.4M | −21% to −28% | ([0.25–0.30) is a slight +2.4% anomaly) |
| mid 0.30–0.80 | ~5.2M | −3% to −22% | takers lose ~everywhere |
| [0.85–1.00) | 1.06M | +1.3% to +2.4% | favorites look +EV — but see caveat |

- **By side:** yes-takers (the cheap-longshot buyers) lose **−20.5%**; no-takers −13.8%. Retail's
  YES-longshot optimism is the dominant transfer.
- **The favorite band [0.85–1.00) showing taker-+EV does NOT contradict the favorite FINDINGS.**
  It's a timing/selection effect — real takers buy favorites *late, tight-spread, near-resolved* —
  not a tradable fixed-horizon edge. Our disciplined ask-aware 24h-out backtest (which a strategy
  must use) still says buying favorites is −EV. Two different measurements; the strategy one governs.

**What this validates:** (1) **no easy taker edge on capital** — confirmed at 3,900× the sample;
(2) **the maker edge is real but modest** — fading the cheap-YES longshot earns **+2.86% net** on
premium overall (the Phase-A thesis, [market-making plan](kalshi-market-making-v1-plan.md) H2). The
where/when/EV breakdown shows that edge is dominated by an inaccessible zone.

**WHERE & WHEN the maker edge lives, and what's realizable (Phase-A EV — `scripts/kalshi_maker_ev.py`):**
Using the CORRECT pre-fee maker P&L (`p − won − maker_fee`), cheap-YES-longshot fade by
time-to-close × sports/non-sports:

| zone | group | premium $ | maker NET edge |
|---|---|---:|---:|
| <1h | SPORTS | 4.2M | **+42.6%** |
| 1–6h | SPORTS | 10.5M | **+41.6%** |
| 6–24h | SPORTS | 2.4M | −4.8% |
| **6–24h** | **non-sport** | **0.23M** | **+21.4%** |
| 1–3d | non-sport | 0.35M | −19.3% |

- **The big edge is final-hours SPORTS** (+42% net on ~$15M premium) — **inaccessible**: fast HFT
  competition (Jump/SIG), fee-rebate-excluded, and the L2 collection found a **deep resting queue**
  (calm-zone non-sports: median 5,721 / mean 46,030 contracts already on the ask).
- **The accessible calm zone (6–24h, non-sports — oil/weather/entertainment) has a real +21.4%
  net edge**, but the addressable volume is tiny: **~$803k/yr** of taker premium (annualized across
  16 shards). Realistic capture behind the queue (2–25%) → **$3k–43k/yr net**.

**Phase-A verdict: a real but marginal edge.** The accessible calm-zone fade is +EV (+21% net per
filled $) yet caps at **single-digit-to-low-tens of thousands $/yr** for a small maker behind a
deep queue — pizza money for a non-trivial build. The large edge (final-hours sports, millions in
volume) needs HFT infra to compete and is fee-excluded. **No path here is a business for a non-HFT
solo operator.**

*Artifacts:* `scripts/kalshi_trades_calibration.py` (headline + by-bucket + by-side + by-series +
by-time-to-close), `data/trevorjs/joined_t0.parquet` (9.5M rows, gitignored),
`data/trevorjs/trades_calibration_report.txt`, `data/l2/collect_20260614.jsonl` (346k L2 snapshots).
Reproduce: build the joined sample (duckdb httpfs over the HF parquet shards), then run the script.

## Weather `[0.40–0.45)` confirmation — RESOLVED (evaporated, as predicted)
Expanded weather-only sweep (top-80 weather series, 4,987 markets, 2,554 rows,
`data/calibration_weather_report.txt`). Pre-registered: real iff calib>0 **and t>2**. Result on
the larger sample: n 53→91, calib **+0.147 → +0.054**, net_ret **+31% → +9%**, drop-top-5
**+19% → +1.2%**, **t 1.90 → 0.72**. It regressed to noise — confirming it was one of the ~7
expected false positives among ~140 buckets. **Taker side is now closed, confirmed three ways:**
(1) candle calibration map, (2) 9.5M real fills, (3) this OOS confirmation.

## Phase-A status (2026-06-14) — first-pass EV done
The maker EV model (`scripts/kalshi_maker_ev.py`, `data/trevorjs/maker_ev_report.txt`) is **done**:
accessible edge real but marginal ($3–43k/yr). A full tick-level adverse-selection backtest would
need weeks of forward L2 through settlement; given the marginal ceiling, that build is **not
recommended** unless targeting the final-hours sports zone with real-time infra. The 2h L2 sample
(`data/l2/collect_20260614.jsonl`, 346k snapshots) stands for any further calm-zone probing.

## Reproduce
```bash
# the full taker map (re-pull):
python3 prediction-markets/scripts/kalshi_calibration.py --top 40 --horizon-hours 24 \
    --max-markets 100 --min-vol 50 --out prediction-markets/data/calibration.jsonl
# re-report from the saved rows (no API calls):
python3 prediction-markets/scripts/kalshi_calibration.py --from prediction-markets/data/calibration.jsonl
# the weather confirmation:
python3 prediction-markets/scripts/kalshi_calibration.py --category "Climate and Weather" \
    --top 80 --horizon-hours 24 --max-markets 120 --min-vol 50
```
Artifacts: `data/calibration.jsonl` (2,460 rows), `data/calibration_report.txt` (the map).

## For a reviewer — where this could be wrong
- **Single horizon (24h).** Only the 24h-before-close snapshot was tested. A different horizon
  (6h/48h) could show different calibration; not yet checked. *(Cheap to add: `--horizon-hours 6,24,48`.)*
- **Candle ask ≠ depth.** The ask captures the spread but not fillable size; real large fills could
  be worse. The map says "no edge even ignoring depth," which only strengthens the negative verdict,
  but a *positive* pocket would need depth-aware confirmation.
- **Both-sides binning.** Each market contributes a YES row and a NO row at mirror prices; this is
  the correct way to build the calibration curve at large n, but at small n per bucket it is
  outcome-colored (that's why tiny categories like Entertainment show ±370% noise — ignore them).
- **t is optimistic.** Resolutions cluster (correlated bets), so true significance is *below* the
  reported t — which makes the "no edge" conclusion *more* robust, not less.
- **Universe = liquid recurring short-dated series.** Thin one-off markets and longer-dated series
  were not swept; an edge could in principle hide there (but those are the hardest to trade).
- **~1% of series carry extra maker fees**, ignored here (immaterial to the taker conclusion).

## Links
**In-repo:** [`scripts/kalshi_calibration.py`](../scripts/kalshi_calibration.py) ·
[`scripts/kalshi_l2_collector.py`](../scripts/kalshi_l2_collector.py) ·
[`scripts/kalshi_pull.py`](../scripts/kalshi_pull.py) ·
[favorite FINDINGS](kalshi-near-certain-favorite-FINDINGS.md) ·
[strategy roadmap](kalshi-strategy-roadmap.md) ·
[market-making v1 plan](kalshi-market-making-v1-plan.md) ·
[full research report](../../research/kalshi-bot-research.md)

**External (the structural facts this reproduces):**
- Becker, *The Microstructure of Wealth Transfer in Prediction Markets* (72.1M trades) —
  https://www.jbecker.dev/research/prediction-market-microstructure
- GWU/UCD, *Makers and Takers* (300k+ contracts, favorite-longshot bias) —
  https://www2.gwu.edu/~forcpgm/2026-001.pdf
- Kalshi fee schedule (the `0.07·p·(1−p)` formula) — https://help.kalshi.com/en/articles/13823805-fees
- Kalshi market-data API (candlesticks, bids-only book) —
  https://docs.kalshi.com/getting_started/quick_start_market_data

---

**One-line bottom line:** Across the entire Kalshi price curve and every liquid category, **no
taker trade is +EV after fees** (the only robust signal is the longshot tail being a reliable
*loser*); taker strategies are dead, and the sole +EV implication — fading that longshot tail as a
*maker* — is exactly what the forward L2 collector is now gathering data to test.
