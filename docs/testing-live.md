# How to test the LIVE path safely

This is how we validate the real-money execution stack (the MCP `review → place → cancel` plumbing in
`rh_mcp.py` + `live_execute.py`) **without risking an unwanted fill**. The engine ships at
`TRADING_MODE=paper`; everything below is a deliberate, reversible step up from that.

> Scope: the agentic account only (`your_account_number`). The MCP rejects any other account, and `rh_mcp.py`
> hard-pins it.

## The safety ladder

Climb one rung at a time; don't skip:

| Rung | `TRADING_MODE` | `LIVE_ARMED` | What runs | Fill risk |
|---|---|---|---|---|
| 1. Paper | `paper` | – | simulated fills (`apply_decision.py`) | none |
| 2. Live **dry-run** | `live` | `0` | real `review_equity_order`, logs "would place", **places nothing** | none |
| 3. **Unfillable-limit probe** | `live` | `1` | real `place` of a never-filling order, then `cancel` | **none by construction** |
| 4. Armed canary | `live` | `1` | real marketable orders, first capped to `LIVE_CANARY_USD` | real (tiny) |

Rung 3 is the important one and the subject of this doc: it exercises the **actual write path against
the real broker** — proving `place` and `cancel` work, order IDs come back, the order rests, and we
can read it back and pull it — while it is *impossible* for the order to execute.

## Why a far-below-market limit on Ford (F) can't fill

A **buy limit** order says "buy, but pay no more than `$X`." If `$X` is set far **below** the current
price, there is no seller willing to hit it, so the order just **rests open, unfilled**, until we
cancel it. It is a real, live, broker-acknowledged order — it simply can never trade.

Ford is the ideal probe symbol:
- **Cheap** (~$10–12/share) → one whole share is a few dollars of (theoretical) exposure, and
  whole-share lots are exactly what the live entry path uses (limit entry + resting `stop_market`).
- **Extremely liquid** → `review`/`place`/`cancel` behave like they will for any real name; no
  thin-book weirdness.
- **Never near the limit** → a limit at, say, **$3 when F is $11** (≈30% of last) has zero chance of
  becoming marketable. Ford has not traded near $3 in the modern era.

So the order tests the entire pipeline and then we cancel it — the account ends exactly as it began.

`scripts/live_smoke_test.py` enforces this: the limit defaults to **30% of the live last** and the
script **refuses to place** anything priced ≥ 50% of last (which could fill), and it **always cancels**
the order in a `finally` block so it never leaves a resting order behind.

## Prerequisites

- The **Robinhood MCP is connected** (the relay spawns a headless `claude` that needs MCP auth — run
  `/mcp` to connect first).
- **Regular OR extended hours.** F trades in the extended sessions (pre-market 04:00–09:30, after-hours
  16:00–20:00 ET), so you can run this probe *right now* after the close. `live_smoke_test.py`
  auto-detects the session and routes the order to `extended_hours` (TIF `gfd`) when appropriate — the
  far-below limit still rests unfilled. Only when the market is **fully closed** (overnight/weekend)
  does the order merely queue for the next session instead of resting; the script warns you.
- The agentic account's **investor profile is complete** — otherwise `place` 400s on the second order
  (a known Robinhood quirk; see CLAUDE.md).
- `.env` has the live knobs: `TRADING_MODE`, `LIVE_ARMED`, `LIVE_CANARY_USD`, `RH_EXEC_MODEL`.

## Run it

### Step A — read + review only (no order placed, totally safe)

```bash
python3 scripts/live_smoke_test.py
```

This pulls a live quote for F (proving the read path + account pin), computes a far-below limit,
runs a **real `review_equity_order`**, prints any broker alerts, and stops. Nothing is placed. Use
this first to confirm the MCP, account, and review path all work.

### Step B — place the unfillable order, then cancel it

```bash
TRADING_MODE=live LIVE_ARMED=1 python3 scripts/live_smoke_test.py --place
```

This does the full cycle:
1. `snapshot` → live F quote (read path)
2. price guard → limit ≈ 30% of last, refuses anything that could fill
3. `review` → real pre-trade review + alert check
4. `place` → a real GTC buy limit at the far-below price; captures the broker **order id**
5. read-back → finds the order via `get_equity_orders`, confirms it is **open / unfilled**
6. `cancel` → cancels it; prints the result (always runs, even on error)

A clean run ends with `PASS: ... Account left flat.`

> Without `LIVE_ARMED=1`, `--place` stops after review and logs "would place" — the same double-gate
> the engine itself uses, so you can't place by accident.

> **Placing NOW, after hours:** this is a *manual one-off* — the script talks straight to the relay,
> so it isn't subject to the engine's market-hours gate. (The automated 5-min planner stays gated off
> after hours by design; it won't place. Only this probe does.) Override the session with
> `--market-hours extended_hours` if auto-detect ever disagrees.

### Manual equivalent (if you'd rather drive the relay directly)

```python
import scripts.rh_mcp as rh, scripts.live_execute as le, uuid
spec = {"symbol": "F", "side": "buy", "type": "limit", "quantity": "1",
        "limit_price": "3.00", "time_in_force": "gtc", "market_hours": "regular_hours"}
print(rh.review(spec))                       # real review, places nothing
ref = str(uuid.uuid4())
placed = rh.place(spec, ref_id=ref)          # rests far below market — cannot fill
oid = placed["order"]["id"]                  # (dig out the id defensively in practice)
print(rh.cancel(oid))                        # clean up
```

## Verify you're flat afterwards

The smoke test's own `[cancel]` line and `PASS ... Account left flat.` are the primary confirmation.
To double-check the broker directly, dump a fresh snapshot (live-mode only — it writes
`data/tick/broker_snapshot.json`):

```bash
TRADING_MODE=live python3 scripts/broker_snapshot.py
cat data/tick/broker_snapshot.json            # inspect open orders + positions
```

Open orders for F should be **empty** (cancelled) and positions unchanged. If a cancel ever fails,
cancel manually in the Robinhood app — a resting far-below limit is harmless but shouldn't linger.

## What this proves — and what it doesn't

**Proves:** MCP connectivity, account pinning, `get_equity_quotes`/`get_portfolio`/`get_equity_orders`
reads, `review_equity_order` + alert parsing, `place_equity_order` with a `ref_id`, order-id read-back,
and `cancel_equity_order`. I.e. the whole write path end-to-end.

**Does NOT prove:** a real *fill*, slippage, partial fills, the resting `stop_market` arming after an
entry fills, or `MAX_*` cap sizing on a live notional. Those only exercise once an order actually
executes — that's **rung 4**, the armed canary (`LIVE_CANARY_USD`-capped first order), which you only
do after rung 3 is green.

## Kill switch (any time)

- `TRADING_MODE=paper` — back to simulation.
- `LIVE_ARMED=0` — live becomes review-only dry-run.
- Disconnect the Robinhood MCP — the relay can't place at all.
