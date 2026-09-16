# CloddsBot — source review (what's portable into Phase A)

> **STATUS 2026-06-16 — REFERENCE REVIEW, no code run, no keys.** Reviews the
> open-source `alsk1992/CloddsBot` (TypeScript, 380★) against our Phase-A maker
> roadmap. Surfaced via a Recogard "7 free Polymarket bots" thread on X; 6/7 of that
> thread are Polymarket-only (owner geo-blocked) — this is the one repo that speaks
> Kalshi. **Verdict: one file of real value (the WS L2 feed); the rest is a parameter
> cheat-sheet or out of scope.** Nothing here changes a single Phase-0 finding.

## Why we looked
Roadmap §1 ([`kalshi-strategy-roadmap.md`](kalshi-strategy-roadmap.md)) makes the
**WebSocket L2 collector** the gating build for the maker / longshot-fade path: Kalshi
serves no historical order book, so the adverse-selection backtest needs the book +
fill tape collected **forward**. Our existing [`scripts/kalshi_l2_collector.py`](../scripts/kalshi_l2_collector.py)
is deliberately **unauth / polled top-of-book** (stays under the "public read-only until
Gate 3" rule). The tick-level `orderbook_delta` upgrade was parked because it needs an
RSA-signed handshake = credentials. CloddsBot is a worked reference for exactly that
parked step.

## File-by-file

### `src/feeds/kalshi/index.ts` — **PORT (translate to Python)**
The asset. Implements the parts that are tedious to get right against live Kalshi:
- **RSA-PSS auth** over `${timestampMs}${METHOD}${path}` → `KALSHI-ACCESS-KEY/TIMESTAMP/
  SIGNATURE` headers (see `kalshi-auth.ts`).
- **`orderbook_snapshot` + `orderbook_delta` with seq-gap detection → resubscribe
  recovery** (`lastSeqBySid`; on gap: clear state, unsubscribe, re-snapshot). This is the
  single trickiest piece of a correct collector and it's done.
- In-memory book reconstruction (price→size maps per yes/no), heartbeat handling,
  **exponential backoff + jitter reconnect with resubscribe**, a `fill` channel, 30s
  staleness tracking.
- Confirms the live `orderbook_delta` channel exists with documented mechanics →
  **de-risks our §1 build** (channel feasibility was an open question in the roadmap).

**Two caveats before porting:**
1. **It's a live feed, not a recorder.** It keeps current book state and emits events but
   never persists the snapshot+delta tape. We port the WS/auth/seq-recovery core and *add*
   a JSONL writer — and **stamp an explicit `gap` marker on every recovery** so replay never
   applies deltas across a re-snapshot.
2. **TS→Python translation, not drop-in.** Subtree is stdlib-Python; the WS+RSA path needs
   `websockets` + `cryptography` (first non-stdlib deps here — flagged). Validate the message
   envelope (`msg` vs `data`, `type` field) against current Kalshi docs; the code carries
   legacy-format fallbacks, implying the API shifted.

### `src/utils/kalshi-auth.ts` — **PORT (small, exact)**
RSA-PSS signer: `RSA_PKCS1_PSS_PADDING`, `SALTLEN_DIGEST`, SHA-256, base64. Maps to Python
`cryptography` `padding.PSS(mgf=MGF1(SHA256), salt_length=PSS.DIGEST_LENGTH)`. Read-only
headers today; **same scheme signs order POSTs at Gate 3.**

### `src/trading/market-making/engine.ts` — **BORROW as default params only**
Clean, pure, testable Avellaneda-lite quoter: fair value (mid / weighted-mid / vwap / ema),
EMA smoothing, inventory skew (long → lower bid/higher ask), volatility-scaled spread,
multi-level quotes with size decay, `shouldRequote` on move-threshold-or-timer, clamp to
[0.01, 0.99].

**But it's the architecture §2 already rejected.** It is **polled** — `evaluate()` fires on
`intervalMs` and re-pulls `getOrderbook()`; it ignores the `orderbook_delta` / `clientOrderId`
stream the feed exposes. Adverse-selection defense is crude (wider spread on vol + timed
cancel/replace) and there is **no queue-position model** — which our Phase-A backtest gate
*requires*. Use the skew/spread/clamp formulas as starting defaults; replace the control loop
with an event-driven, queue-aware one.

### What isn't here (and is the actual gate)
- **No adverse-selection-aware backtest.** The real Phase-A gate (fill only when fair value
  reaches/crosses the resting quote; model queue; net live fees; drop-top-N). CloddsBot's
  `onTrade` P&L is naive mark-to-fair-value — no fees, no queue, no settlement. We build this
  ourselves on our own tape regardless.
- **Kalshi order placement is stubbed.** Batch placement is Polymarket-only
  (`supportsBatch = platform === 'polymarket'`); Kalshi falls back to sequential
  `makerBuy/makerSell` behind an execution abstraction not shown. Gate-3 territory anyway.

## Decision table

| Component | Port? | Why |
|---|---|---|
| Kalshi WS feed: auth, subscribe, snapshot+delta, **seq-gap recovery**, reconnect | **Yes — translate, add JSONL recorder + gap markers** | Builds §1, the gating prerequisite; gap recovery already correct |
| `kalshi-auth.ts` RSA-PSS signer | **Yes — small, exact** | Saves doc-spelunking; same scheme for order POSTs at Gate 3 |
| `engine.ts` quoting math | **Borrow as default params** | Sound skeleton, but polled + queue-blind = the architecture §2 rejected |
| Adverse-selection backtest | **N/A — doesn't exist** | The real gate; we build it on our tape |
| Execution / order placement | **No** | Polymarket-batch-first, Kalshi stubbed; Gate-3, research-only now |

## The Gate-3 line (important)
The WS `orderbook_delta` collector **needs a Kalshi API key + RSA private key** — a credential,
not public data. Our existing unauth collector avoided this on purpose. So the Python skeleton
([`scripts/kalshi_l2_ws_collector.py`](../scripts/kalshi_l2_ws_collector.py)) is **demo-only,
parked behind a secrets decision**: it is reference code, not wired into any scheduler, and is
**not to be run with live credentials until the owner clears Gate 3** (separate secrets +
kill switch per `CLAUDE.md`). Until then, forward collection continues on the unauth
top-of-book poller; the WS upgrade is the fidelity step we take *with* the rest of the
Gate-3 maker build, not before it.

## Net
~1 file of value: the collector. It cuts the riskiest part of §1 (correct delta application +
gap recovery) from "design it" to "translate a working reference," and confirms the channel
exists. The quoting engine is a parameter cheat-sheet, not an edge; the part that decides
whether Phase A is real — the adverse-selection backtest — isn't here and never could be.
Strictly reference; nothing run, no keys.
