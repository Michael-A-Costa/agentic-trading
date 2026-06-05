# AI / Agentic Trading Landscape — 2026 H1 (Dec 2025 – Jun 2026)

**To:** Michael (account owner) · **From:** Head of Quant Research · **Re:** What changed in the field, what's noise, and exactly what we change in our code

> **Live-state scope note (read first).** As of this writing `.env` is `TRADING_MODE=live`, `LIVE_ARMED=1`, `MAX_TOTAL_EXPOSURE_PCT=0.19` (the temp first-live-test cap), `MAX_POSITION_PCT=0.05`, `MAX_OPEN_POSITIONS=10`, intraday entry at `SIGNAL_THRESHOLD_PCT=3.0` / `REL_STRENGTH_PCT=3.0` / `MAX_HOLD_MIN=120` + EOD-flatten. **We are placing real orders, intraday, on the horizon our own daily-bar backtest finds anti-predictive.** That tension is the spine of this memo. One caveat on the proof itself, stated up front and revisited in §4: our backtests resolve only to **daily bars**, so they condemn the *overnight* edge of the intraday signal but **cannot see the ~5-min microstructure the live engine actually trades** — the proof is at a coarser resolution than the thing it indicts.

---

## 1. TL;DR

- **The field spent H1 2026 confirming our two findings from the outside.** Credible new results echo what we proved on our own data: (a) general LLM intelligence ≠ trading edge, and (b) the surviving edge AND the cost-efficient regime both live at the **multi-day/overnight swing horizon our engine architecturally avoids** by EOD-flattening. De Boer et al. (Nov 2025) is the cleanest external explanation of *why* a single-name intraday pop reverts: stock-specific return reverts short-term, only style/industry trend persists. *(SSRN 5716502 — as-reported, not independently reproduced.)*
- **Survivorship/look-ahead is the field's #1 killer and our #1 blocker — and it is now half-solvable keyless.** FINSABER (KDD 2026) re-ran the famous LLM agents survivorship-corrected and buy-and-hold won. The free PIT S&P constituent lists (fja05680, MIT, updated 2026-01-17) unblock the *list* half. **But we live-verified the price half is blocked on our feed:** Cboe's CDN **403s on LEH, BSC, AABA** (confirmed this session) while serving live names — so we can *name* the dead constituents but not *price* the 2008-era ones keyless.
- **The headline +12.1%/yr, t=3.05 cross-sectional momentum result is now pinned to an exact, reproducible cell — and it is the optimistic, survivorship-inflated number until proven otherwise.** `python3 scripts/backtest_xsection.py --lookback 126 --skip 21 --topk 5 --rebalance 21 --cost-bps 10` → mom CAGR 28.5%, EW CAGR 16.4%, **spread +12.1%/yr, t=3.05, MaxDD 61.0%**, over 2004-08-04→2026-06-03 (262 monthly rebalances). **This is top-5 / 126d (≈6-mo) — NOT the script's top-10 / 252d default, which is much weaker (+3.2%/yr, t=1.15).** External counterweights: Kumar (Feb 2026) reports naive 12-1 on the S&P 500 at Sharpe −0.23, −81% MaxDD net 2006–2024 *(as-reported)*; Estrada/AllocateSmartly argue pure *relative* momentum has been flat since ~2008 and the apparent edge is mostly equal-weighting. Our EW-spread benchmark already controls for the latter — that is to our credit — but the universe bias remains.
- **Durable momentum = residualize + vol-scale + absolute-momentum gate. Naked relative 12-1 is fragile.** The 2025-26 literature converges (Robeco RM_MOM, Barroso–Santa-Clara, Hanauer-Windmüller). Each lever independently is argued to roughly halve momentum's drawdown — directly the **61.0% MaxDD** in our pinned cell.
- **Backtest rigor is non-negotiable *before* we believe any knob we add.** Deflated Sharpe Ratio (≈100 correlated trials can manufacture a high Sharpe from noise) and Combinatorial Purged CV with purge+embargo are the gate. We are about to add construction knobs to the momentum sleeve; without DSR/CPCV we re-discover survivorship bias with extra ceremony.
- **The LLM's job is due-diligence/risk-gating, not alpha — keep `decide.py` as a commit/reject gate, never backtest it on pre-cutoff dates.** LiveTradeBench: LMArena↔return Spearman ≈ 0.054. Profit Mirage: 51-62% Sharpe decay past the knowledge cutoff *(both as-reported)*. Citadel's CTO: everyone prompting the same models on the same public text = crowded, decayed signal — and our liquid top-gainers sit in the *most* crowded zone.
- **Execution hygiene is a regulatory + account-mechanics front.** FINRA's 2026 report demands process-reconstruction telemetry (our fat `engine-log.jsonl` is the right instinct). The PDT repeal (RN 26-10) is **margin-only and does not help us** — account `your_account_number` is confirmed **cash** (`get_accounts`), so the binding constraint is **T+1 Good-Faith Violations**, which our intraday-recycle + EOD-flatten pattern is the textbook way to trip.
- **Net recommendation (§4): stop expanding the intraday engine; stand up a parallel multi-day swing sleeve as the research track, gate it behind the survivorship test, and degrade the live intraday engine to a smaller, regime-gated, defensive posture.** With the daily-bar caveat noted, slippage/stop tuning does not fix the *overnight* horizon — that is settled by us and now by several independent external lines.

---

## 2. The Landscape, by Category

### 2a. LLM / Multi-Agent

**Robinhood Agentic MCP (our rails).** Launched 2026-05-27: isolated account, real-time P&L feed, one-tap kill switch, optional per-trade preview. Baseline, not a competitor. **Confirmed in-repo via `get_accounts`:** account `your_account_number` is `type:"cash"`, `self_directed` — load-bearing for the GFV discussion in §2g/§4/P3. On native stops: our `live_execute.py` emits `type=="stop_market"` (line 242) and treats it as **best-effort with a synthetic fallback** (lines 195-200, 343-349); CLAUDE.md flags the live path as verified end-to-end only once (2026-06-04). *I did not re-confirm the full `review_equity_order` type enum against the live MCP schema in this revision — so I am NOT asserting "native stops verified." The operative fact is: our code requests a resting stop and degrades to synthetic if the broker doesn't honor it.* This flaky-stop dependency is exactly what P2's overnight design leans on, and is called out there.

**Vibe-Trading (HKUDS, MIT).** The closest public peer — same Robinhood MCP rails. Its safety model (user mandate: universe/order size/exposure/leverage/daily cap + filesystem kill switch + append-only audit ledger) is consensus and **matches what we already have** (`.env` caps + MCP disconnect + `engine-log.jsonl`/`trades.jsonl`). It disclaims the Robinhood path as unverified against a real broker and makes no live-performance claim. *Verdict: validates our guardrail architecture; nothing to copy on alpha.*

**FINSABER (KDD 2026 D&B).** Re-run FinMem/FinAgent over 2004-2024 with PIT constituents incl. delisted names + real commissions, and the reported LLM edge evaporates (B&H beats the agents) *(as-reported)*. *Verdict: credible — and the literal protocol template for our §5 survivorship test. **Caveat:** FINSABER tests timing agents, NOT cross-sectional momentum, so it neither blesses nor damns our factor; citing its numbers about our sleeve would be a category error.*

**The Alpha Illusion, Profit Mirage/FactFin, Agentic-Trading survey.** Three independent 2026 audits converging: of the action-emitting LLM-trading studies surveyed, the vast majority modeled neither transaction cost nor survivorship, and ~none reached top reproducibility *(as-reported counts; surveys of others' failures, not evidence any technique works)*. The survey's *Outcome Embargo* (hide outcome-at-time-t from retrieval until t+k) is a precise, cheap pre-emptive fix for a latent leak in *our* `stock_memory.py` — see §4 P4. *Verdict: adopt the discipline (free hygiene).*

**RD-Agent / RD-Agent(Q) (Microsoft, NeurIPS 2025).** Multi-agent factor+model co-optimization, claims ~2x ARR with fewer factors. Built on Qlib and heavyweight; over-engineered for our 60-name keyless sleeve. *Not now.*

### 2b. Deep-RL

**FinGPT-sentiment + TD3 (Oct 2025).** The most useful number in the sweep for us *(as-reported, not reproduced)*: a daily LLM-sentiment long-short returns ~16.66%/yr gross but **collapses to ~0.13%/yr after 5bps costs.** RL long-only nets ~23.65% after 10bps but at ~52% turnover. *Verdict: quantifies our slippage skepticism — a daily LLM edge is real gross and ~fully eaten by costs; lower turnover (multi-day holds) is what survives.* Zero-shot time-series foundation models (TimesFM/Chronos/Moirai) on daily returns are reported **anti-useful** (negative R², sub-50% directional) and irrelevant to our no-GPU, keyless setup — see §3.

### 2c. Factor / Momentum (the productive category for us)

The real upgrades, all on the **swing sleeve, not the live engine.** Each maps to a specific line; the gate logic notes below correct the draft's pointer.

| Technique | Source | Verdict | Maps to |
|---|---|---|---|
| **Residual/idiosyncratic momentum** — rank on SPY-residualized 12-2 return ÷ residual vol | De Boer Nov 2025; Hanauer-Windmüller; Robeco | promising-unproven | replaces ranking key at `backtest_xsection.py:129` (`p_skip/p_back-1`) |
| **Absolute-momentum gate** — force the cash leg when SPY<200d SMA at rebalance | AllocateSmartly Dec 2025; AQR | credible-edge | **inside `run()`'s rebalance loop**: read SPY MA200 at `spy_dates[i]`; when below, **skip the pick / set the period return to the cash-leg return (0 or a T-bill proxy), NOT just suppress `mom_r` after cost at line 140** |
| **Constant-vol overlay** — scale sleeve to 12% target, w=0.12/σ̂ from 126d realized var, W_MAX=1.0 (cash account, no lever) | Barroso–Santa-Clara 2015 | credible-edge | post-process on `run()`'s `mom` series |
| **Clenow slope×R² ranking + >15% gap exclusion + ATR sizing** | QuantConnect port | promising-unproven | ablation fork |
| **Sector-ETF rotation** (9 SPDRs, 12-1, top-3) | Faber/Quantpedia | promising-unproven (as **survivorship-free diagnostic**) | Test 1 |

The convergent story: **strip beta (residual), scale to vol, add a cash leg** — each independently argued to roughly halve the drawdown behind our **61.0% MaxDD** cell. Clenow's >15% single-day gap *exclusion* is the conceptual inverse of our discovery screen (we *buy* ≥3% pops; he *disqualifies* big gappers) — independent corroboration that absolute pops are anti-predictive *at the daily/overnight horizon*. **Timing caveat:** JPM Factor Views 2Q 2026 flags momentum dispersion as the widest since 1990, NEUTRAL — an argument for vol-scale + absolute-gate de-risking *before* any capital.

### 2d. Sentiment / News / Catalyst

A *real* daily news→LLM-score→next-day long-short edge exists in the literature (Kirtac-Germano OPT Sharpe ~3.05 gross *(as-reported)*) — but it is **overnight-hold** (our avoided horizon), **gross of costs** (FinGPT-TD3 shows that collapses at 5bps), and **crowded** (Citadel). The survey's load-bearing warning: news timestamps usually record *publication* time, not pipeline-availability time — a leakage vector that shows **phantom edge**. **Keyless catalyst source that exists:** GDELT (free, ~15-min cadence) + local FinBERT — the news analogue of our keyless Cboe OHLCV, suited to a *daily* catalyst overlay. GDELT's structured Events coverage is solid only from ~March 2026 forward, so it can't backfill a long historical study. *Verdict: a catalyst overlay is plausible but is a swing-horizon, daily-cadence thing — not a rescue for the intraday entry.* **This is precisely the thesis of our own in-flight `scripts/backtest_gap_drift.py` (PEAD) — see §5 Test 0.**

### 2e. No-code / Composer

The **TQQQ-For-The-Long-Term archetype**: 200d-SMA regime gate → default long, 10d-RSI>80 overheat veto, RSI<30 lean-in, below-SMA → T-bills. **Strip the 3x-LETF juicing** (single-regime curve-fit) and the transferable primitives are two we can use: a **regime gate** and an **RSI-overheat buy-suppression veto** — but both belong on the **swing** sleeve, because being more selective on the *intraday* horizon makes 1-day expectancy *worse* (we proved this on daily bars). The honest counterweight: Composer's daily-ranked symphonies are a textbook data-snooping surface (leaderboard Sharpes should be treated as snooped; anecdotal blow-ups circulate but are not load-bearing). *Verdict: copy the gate, never the leverage.*

### 2f. Frameworks / Data

- **PIT S&P constituents, keyless:** fja05680/sp500 (MIT, dated snapshots, dead names with removal dates, updated 2026-01-17). **Solves the list half of our #1 blocker for free.** Reliable from ~2001; pre-2022 membership reconstructed from Wikipedia revisions (date-error risk).
- **The residual wall (live-verified this session):** keyless *price* data for delisted names is partial. **Confirmed 403** on LEH/BSC/AABA (2008 GFC names gone). **Confirmed 200** for ~2012+ delistings: TWTR (2262 bars, tail $53.35 near the $54.20 buyout), FRC (3194 bars). yfinance silently returns empty frames; Stooq is login-gated; Norgate/CRSP are paid. **Critical trap, live-verified:** Cboe **freezes the tail of a halted name at the halt price** — SIVB returns 4843 bars with the last three closes all **$106.04 (2023-03-22/23/24)**, i.e. neither zero nor the resolution value. Any Test-3 join MUST override every delisted series' terminal return with a Form-25-anchored resolution value and **hard-truncate at the removal date** (ticker reuse — WB, SLE — will otherwise corrupt joins).
- **SEC EDGAR Form 25 / 25-NSE, keyless** (`efts.sec.gov`, User-Agent only) gives delist *date* + ticker — anchors the −100%/resolution bar, not prices. Do **not** pull `janlukasschroeder/sec-api-python` (paid, key-gated, violates no-key rule).
- **PyBroker (Apache+Commons-Clause):** walk-forward + bootstrap CIs, can ingest our Cboe cache. Useful for rigor, but **does NOT ship CPCV/purge/embargo** — that layer is custom NumPy on our side. Backtrader is legacy/unmaintained — avoid.

### 2g. Backtest Methodology

In priority order for us:
1. **Point-in-time survivorship-free universe** — the binding gate; necessary before *any* other number means anything. FINSABER protocol.
2. **Deflated Sharpe Ratio** — collapse correlated trial count M→N̂ via the average-correlation formula (our lookback cells are ~95% correlated, so raw M over-penalizes). **Adopt** — pure Python/scipy; the acceptance gate on the knob sweep we're about to run.
3. **CPCV with purge+embargo** — turns the single-path sweep into a distribution; select on the 10th-percentile PSR, compute PBO. **Prototype** — ~120 lines NumPy.
4. **Outcome Embargo** for `stock_memory.py`. **Adopt pre-emptively.**

**Hard rule:** DSR/CPCV/embargo address multiplicity, leakage, non-Normality — they do **NOT** touch survivorship or cost-realism. A high DSR on our survivorship-biased universe is a *false all-clear*. Sequence: survivorship FIRST, then DSR/CPCV on the de-biased curve.

---

## 3. What to Ignore (hype/scam patterns)

- **Vendor return claims:** Tickeron, BATL, Composer leaderboard, FinRL-X, TradingAgents Sharpe 5-8 — gross / regime-lucky / data-snooped upper bounds; TradingAgents' own high Sharpe is a single ~3-month quiet-window tech backtest the authors flag.
- **The viral "95% of hedge funds went agentic by April 2026"** — traces to AIMA *GenAI-usage* survey figures relabeled as autonomous execution. Usage ≠ execution. Two Sigma's 2026 outlook: "the next year won't be so much about LLMs making trades."
- **Zero-shot TSFMs on daily returns** — anti-useful (negative R²).
- **AI-branded signal-bot/Telegram groups** — fraud markers per Chainalysis/TRM aggregates (cited rhetorically, not as load-bearing quant evidence). Any "AI guarantees X%" is a red flag.
- **"Free" data that isn't:** Finnhub/EODHD/FMP (key-gated), yfinance/Stooq for delisted prices (fail the survivorship test). Withdrawn arXiv 2512.11913 — do not cite.
- **GitHub stars ≠ edge.** `wshobson/mcp-trader` archived; `maverick-mcp` is yfinance-based (conflicts with our keyless constraint).
- **Naive vanilla 12-1 on large-caps** — Kumar reports Sharpe −0.23, −81% MaxDD net *(as-reported)*. Our pinned cell is the optimistic survivorship version. Don't ship raw relative 12-1.

---

## 4. Plan to Update OUR Workflow

**Core tension, blunt:** `tick_context.py → decide.py → executor` trades the **intraday absolute-pop horizon** that our `backtest_signal.py` finds anti-predictive *at the daily/overnight resolution it can measure*. Our only defensible edge (`backtest_xsection.py`, +12.1%/yr over EW, t=3.05, but 61% MaxDD) is **multi-day overnight-hold momentum the live engine refuses to hold.** **Known blind spot (do not overclaim):** daily bars cannot rule out an *intraday* microstructure edge (open→close, VWAP reversion) because they never resolve below one day; the owner won't buy minute data (keyless), so we state this as an unexamined hole rather than claim a clean kill. Ordered by impact:

### P0 — De-risk the live engine (mostly a *tuning* change, not new construction)
**Today's regime read (from the SPY cache, last bar 2026-06-03, close 754.24):** MA50 709.82, MA200 682.87, 20d return +4.21% → classified **`up`**. So both the existing gate and the proposed softer one are **OFF right now** (correctly — the tape is in an uptrend). P0 is a *forward* safeguard, not an immediate brake.

1. **Loosen the existing downtrend gate — do NOT build a new one.** `market_conditions.py:263` already classifies a *confirmed* downtrend (below MA50 **AND** below MA200 **AND** neg 20d return) and lines 372-374 already override posture to `risk_off` ("entries off"); `tick_context.py:389` already consumes `posture=='risk_off'` to gate entries. **The actual delta:** add a *softer, earlier* `risk_off` trigger on a plain **SPY<MA200** cross (one condition, not three) as an additional classification in the `daily_trend` classifier (~`market_conditions.py:263`) or the posture override (~line 372). An engineer must NOT duplicate the gate in `tick_context.py` — it exists.
2. **Keep exposure capped low.** `MAX_TOTAL_EXPOSURE_PCT=0.19` is the temp test cap; **do not revert to 0.80** for the intraday engine. The intraday book should be the *small* book.

### P1 — Stand up a parallel swing sleeve as a SEPARATE research track (the actual fix)
Do **not** rearchitect the live engine yet. Build `backtest_xsection.py` (and the on-thesis `backtest_gap_drift.py`) into validated, drawdown-controlled constructions first:
- **Cheapest sanity floor (zero new code, 5 min):** the current `UNIVERSE` is 60 hand-picked survivors. **Drop the 10 best-performing names and re-run the pinned cell** — if the +12.1% spread survives, the bias is bounded; if it craters, that bounds the artifact *before* any fja05680 pull.
- **Add the absolute-momentum gate inside `run()`'s rebalance loop** (read SPY MA200 at `spy_dates[i]`, force the cash leg below it — show the cash-leg return, don't just suppress `mom_r` at line 140). Pre-register success on **MaxDD** (target 61%→<35%), not CAGR. ~15 lines.
- **Fork the ranking key to residual momentum** — replace `backtest_xsection.py:129` with SPY-residualized 12-2 return ÷ residual vol (rolling ~756d CAPM OLS ending at `d_skip` to preserve the skip-month; standardize by *trailing* residual std, never full-sample — that's a leak). New `scripts/backtest_resmom.py`, reuse `load_closes/equity_stats/tstat`. SPY 2004-2026 + the full-history names are on disk.
- **Layer the constant-vol overlay** (w=0.12/σ̂, W_MAX=1.0) on the *gated, residualized* series. Pre-commit target=12%; report the 8/10/12/15% band as robustness, never the best cell.
- **Sequence is mandatory:** all three run on the survivorship-biased universe and are **screens only.** Nothing goes to capital until §5's PIT test passes.

### P1b — Two-book exposure accounting (architectural gap — design BEFORE any swing capital)
The engine has exactly **one** global cap set. Both executors enforce a single `MAX_TOTAL_EXPOSURE_USD` (`live_execute.py:204-226` in `check_entry_caps`; `apply_decision.py:189-201`) and a single `MAX_POSITION_USD`/`MAX_OPEN_POSITIONS`. If a swing sleeve ever trades real capital alongside the intraday engine, **they contend for the same budget** — there is no two-book accounting today. **Decide and encode before P2 goes live:** either (a) carve `MAX_TOTAL_EXPOSURE_PCT` into named sub-budgets (`INTRADAY_EXPOSURE_PCT` + `SWING_EXPOSURE_PCT`, summing ≤ the global cap), each checked independently in `check_entry_caps`, or (b) run the swing sleeve in a *separate* state file with its own caps and have the live executor read both before sizing. This is a real design item, not a config tweak.

### P2 — If the swing sleeve survives §5, migrate the live engine's *holding* horizon
Only then: replace EOD-flatten + `MAX_HOLD_MIN=120` with **overnight holds + monthly rotation + resting `stop_market` GTC**. **Unresolved broker dependency (flag, don't assume):** our own `live_execute.py` treats resting stops as best-effort with a synthetic fallback (lines 195-200, 343-349), CLAUDE.md notes the live path was verified only once (2026-06-04), and the investor profile must be completed or `place` 400s on the 2nd trade. An overnight design that *depends* on a reliably-resting GTC stop inherits a dependency the repo itself flags as flaky — validate resting-stop persistence across an overnight boundary on the canary before sizing up. **Operational consequence the recommendation implies:** a monthly-rotation swing strategy does **not need a ~5-min cron** — it needs one decision near the close on rebalance days. `run_trading_tick.sh` cadence, the DD token budget (`MAX_DD_CANDIDATES`, Sonnet ~85s/name), and the whole tick economics change; budget for that re-architecture, don't bolt swing onto the 5-min loop.

### P3 — Cash-account settlement guard (operational foot-gun, do before scaling exposure)
Account `your_account_number` is confirmed **cash** (`get_accounts`). PDT repeal (RN 26-10) is **margin-only — does not help us.** The binding risk is **GFV/T+1**: intraday buy-with-proceeds-then-sell + EOD-flatten rotation is the textbook pattern; **3 GFVs in 12 months → 90-day settled-cash lockout** that bricks the engine. Today the only cash gate is `notional > buying_power` (`live_execute.py:225`) / `notional > state["cash"]` (`apply_decision.py:226`), and Robinhood's `buying_power` is instant/unsettled — it would happily authorize a GFV. **Fix:** size new entries against a **settled-cash ledger** (decrement on buy, credit sale proceeds T+1 on a real market calendar), not raw `buying_power`. The reactive `"UNSETTLED"` keyword in `BLOCKING_ALERT_KEYWORDS` (`live_execute.py:265`) is a backstop, not the primary control — GFVs are assessed post-hoc.

### P4 — LLM-stage hygiene (cheap, do alongside)
- **`decide.py` stays a commit/reject gate — do NOT spend tokens upgrading the model expecting alpha.** Its job is rejecting names with no catalyst / pending dilution / earnings-gap traps. If a news overlay ever ships, point it at names *below* institutional attention (crowding).
- **Never backtest `decide.py` on pre-cutoff dates** (memorization decay). Our `backtest_signal.py`/`backtest_xsection.py`/`backtest_gap_drift.py` are SAFE (no LLM in loop) — keep the full 2004-2026 window.
- **Outcome Embargo for `stock_memory.py`:** verified it stores `decision/conviction/summary` but **no outcome/pnl field** (`get_note`, line 73, returns last_decision/summary only) — **no live leak today.** The leak appears the instant anyone adds a realized-outcome field and replays it. Pre-emptively: any outcome field carries `outcome_known_ts` and `get_note()` withholds it unless sim-clock ≥ t+k (no-op live).
- **Conviction→size calibration ledger (long-horizon, NOT a near-term fix).** `dd_prompt.txt:81` maps `high=1.0 / medium=0.6 / low=0.35 × MAX_POSITION_USD` with zero calibration. **Reality check:** the engine has produced ~8 *real* paper round-trips so far (all 2026-06-04, all same-day EOD-flatten) plus 2 unit-test rows in `data/trades.jsonl`. At a ~5-min cron with small `MAX_DD_CANDIDATES` and frequent rejects, reaching **N≥50 committed-and-exited** trades is **months** away. So: start *logging* `{conviction, dollar_amount, realized_pnl, regime}` per round-trip now, but treat the recalibration ("if high-conviction doesn't out-realize low, flatten to one fixed size") as a deferred item gated on data accrual, not an actionable June task.

---

## 5. Backtest Plans

Prioritized by the survivorship gate, reusing existing harnesses. **Keyless-runnable vs needs-new-data is marked.**

**TEST 0 — Catalyst gap-drift / PEAD [KEYLESS, already scripted, run this week].** *Fold in the in-flight `scripts/backtest_gap_drift.py` (currently untracked in the working tree) — it is the most on-thesis test we have and the §5 plan previously omitted it.* It tests a **multi-day, catalyst-driven, agent-scalable** edge (overnight gap ≥X% on a volume spike → forward H-day drift from the gap-day *close*) that explicitly dodges the 1-day reversal, across a LARGE (control) and a MIDCAP (less-covered) universe to read the inefficiency gradient PEAD predicts. An LLM reading filings across thousands of names is the natural PEAD harvester, so this is arguably *more* agent-native than 12-1. Run `--sweep` (gap×hold grid, both universes); pass = MIDCAP gap-day drift beats its all-day baseline with |t|≥2 net of the script's 15bps/side. **Caveats already baked into the script:** survivorship+recency bias on MIDCAP (trust the LARGE control), and entry modeled at the gap-day close (daily bars). **Zero new data.**

**TEST 1 — Sector-ETF momentum as a survivorship-FREE proxy [KEYLESS, run this week].** *The cheapest decisive falsification of the momentum thesis.* Swap `UNIVERSE` (`backtest_xsection.py:45-52`) for the 9 SPDR sectors `[XLK XLF XLE XLV XLY XLP XLI XLB XLU]`. **Live-verified this session: XLK and XLF each serve 5642 clean Cboe bars (HTTP 200); these tickers are NOT yet in `data/backtest/history/` (101 files, none SPDR) — they will be fetched on first run, not read from cache.** Exclude XLRE/XLC (too young; sub-window only). Run 12-1 (lookback 252/skip 21), top-3, monthly, EW-of-sectors benchmark. **The wrapper never delists — no PIT universe to reconstruct.** If sector momentum beats EW-of-sectors with |t|≥2 net of 5bps, the thesis survives a clean test; if flat, the stock-level +12.1% was largely survivorship. Either outcome is decision-useful. **Zero cached data; ~9 small fetches.**

**TEST 2 — Survivorship-hole quantifier [KEYLESS, run this week].** Pull fja05680 PIT constituents into `data/backtest/sp500_pit/` (raw.githubusercontent, User-Agent only; **resolve the current filename first** — the notes' URL already 404'd). Build `members_asof(date)`. For each monthly rebalance 2010→2026, tabulate per-year: count of then-members now delisted, and their names. **Measures the bias magnitude with no price data** — the headline drop-count that tells us whether +12.1% is plausibly real or mostly artifact. **Zero new price data.**

**TEST 3 — Full survivorship-free 12-1 re-run [NEEDS NEW DATA — the gate].** At each rebalance, draw candidates from `members_asof(d)` incl. names that later died; **force the delisting/resolution return** (acquisitions ATVI/TWTR near premium, failures SIVB/FRC near zero) anchored to the Form-25 date. **Data-handling hazard, live-verified and MANDATORY to handle:** Cboe **freezes the tail of a halted name at its halt price** — SIVB's last three closes are a flat **$106.04** (2023-03-22/23/24), not zero, not the resolution value. **Override every delisted series' terminal return with the Form-25-anchored resolution; never trust the raw tail; hard-truncate at the removal date** (ticker reuse otherwise corrupts joins). **Blocked keyless on the price half for 2008 names** (LEH/BSC/AABA 403, verified). Honest scope: Cboe serves ~2012+ delistings, so this is a **~2013-2026 survivorship-corrected test (~13y, one mostly-bull regime), NOT the full FINSABER span** — and a momentum edge spanning only one bull regime is itself suspect. **Edge is REAL if (mom−EW) spread t-stat stays >~2; if it collapses toward 0, it WAS the bias.** **Extra data:** a one-time free/community dump of delisted daily OHLCV (verify split-adjustment), or accept Test 2's drop-count as the verdict.

**TEST 4 — Residual + vol-scale + absolute-gate ablation [KEYLESS, after Tests 1-2].** New `backtest_resmom.py`. Columns: raw 12-1 / residual 12-1 / vol-scaled residual / EW. Pre-registered pass: drawdown materially cut from the 61.0% baseline, return not lost, (sleeve−EW) t>~2, all on a **stress cost of 25bps/side** (not the current 10). **Zero new data**, but "directionally supported, survivorship-unconfirmed" until Test 3 clears.

**TEST 5 — DSR + CPCV on the de-biased curve [KEYLESS, last].** Write `scripts/deflated_sharpe.py` (port Bailey-LdP Snippet 1; sanity-check against the paper's worked example — if it doesn't reproduce, the port is wrong, stop). Retain every sweep config's `diffs` series; compute N̂ from cross-config correlation. **Acceptance: 10th-pct PSR>0.95 AND PBO<0.5.** Run **after** the survivorship fix — a high DSR on the biased universe is a false all-clear.

**TEST 6 — Cost/GFV simulation [KEYLESS, but reframed].** *The draft called this a replay of `data/trades.jsonl`; that file holds only ~8 real round-trips (all same-day EOD-flatten, 2026-06-04) plus 2 unit-test rows — too thin and too uniform to quantify GFV exposure.* **Reframe as a synthetic settlement simulator:** generate a turnover schedule from the engine's *mechanical* pattern (`MAX_HOLD_MIN=120` recycle + EOD-flatten + cooldown + `MAX_OPEN_POSITIONS=10`), drive it through a T+1 settled-cash model (US market calendar, proceeds credited next business day, buys decrement a settled ledger), and count would-be GFVs per rolling 12 months + entries that'd be blocked for insufficient *settled* funds. Replay the 8 real round-trips as a secondary sanity input once the model exists, and **re-run on the real ledger periodically as it grows.** **Zero new data; not gated on accumulating trades** (the synthetic schedule is the input).

> **Sequencing: Tests 0, 1, 2 this week (all keyless, decisive-or-diagnostic), plus the 5-min drop-the-best-10 sanity floor (P1).** They may by themselves kill or de-risk the whole momentum-rewrite before we source a delisted-price dump. Test 6 (synthetic) runs anytime. Test 3 is gated on finding one free delisted-OHLCV source. Tests 4-5 follow.

---

## 6. Sources

**Academic (primary, all as-reported / not independently reproduced):** FINSABER (arXiv 2505.07078) · The Alpha Illusion (2605.16895) · Agentic Trading survey (2605.19337) · Profit Mirage/FactFin (2510.07920) · LiveTradeBench (2511.03628) · StockBench (2510.02209) · De Boer "Which Trends Are Your Friends?" (SSRN 5716502) · Kumar "Does Classic 12-1 Still Work?" (SSRN 5367656) · Robeco RM_MOM (SSRN 5561720) · Kirtac-Germano (2412.19245) · FinGPT+TD3 (2510.10526) · Deflated Sharpe (Bailey-LdP).

**Methodology / practitioner:** AllocateSmartly (Estrada critique) · CFA Institute (momentum framework) · Quantpedia (momentum-crash fixes) · JPM Factor Views 2Q26 · CPCV-with-code (Quant Beckman) · Two Sigma 2026 Outlook · Citadel CTO on crowding (hedgeco).

**Data / tooling:** fja05680/sp500 (PIT, MIT) · hanshof/sp500_constituents · GDELT open data (AWS) · PyBroker · RD-Agent · Vibe-Trading.

**Platform / regulatory:** Robinhood Agentic launch (newsroom/TechCrunch) · FINRA RN 26-10 (PDT repeal, margin-only) · FINRA 2026 Oversight Report · Fidelity GFV/T+1.

**In-repo, live-verified this session (load-bearing):** `get_accounts` → account `your_account_number` `type:"cash"` · Cboe CDN 403 on LEH/BSC/AABA, 200 on XLK(5642)/XLF(5642)/TWTR/FRC, SIVB frozen tail $106.04×3 · `backtest_xsection.py` pinned cell (top-5/126d → +12.1%/yr, t=3.05, 61.0% MaxDD) · single global exposure cap in `check_entry_caps` · `stock_memory.py` has no outcome field · `dd_prompt.txt:81` conviction map · current SPY regime = `up` (754.24 vs MA200 682.87).

---

**One-line bottom line:** The field's 2026 verdict and ours rhyme — the intraday *overnight* entry is dead at daily resolution (the ~5-min microstructure is an admitted blind spot), the defensible edge is multi-day swing (residualized, vol-scaled, regime-gated; pinned at +12.1%/yr, t=3.05, 61% MaxDD), and it is unproven until it survives a point-in-time universe. **De-risk by loosening the existing gate (P0), validate the swing + PEAD sleeves keyless this week (Tests 0/1/2 + the drop-best-10 floor), design two-book accounting before any swing capital (P1b), and don't move money until survivorship clears.**