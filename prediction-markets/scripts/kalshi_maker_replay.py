#!/usr/bin/env python3
"""
kalshi_maker_replay.py — adverse-selection-aware maker backtest over an L2 tape (v1).

  THE PHASE-A GATE. Replays a forward-collected book+trade tape (kalshi_public_tape.py at L1,
  or kalshi_l2_ws_collector.py at full depth post Gate 3) and scores a passive-maker /
  longshot-fade policy with the only honest fill rule:

    FILL A RESTING MAKER QUOTE ONLY WHEN FLOW CROSSES IT, AND ONLY AFTER THE QUEUE AHEAD
    OF US AT OUR PRICE IS CONSUMED.

  v1 hardening (2026-06-16), so the eventual number is trustworthy:
   * Stats aggregated to PER-MARKET round-trips, not per-fill — many fills in one market are
     ONE bet; per-fill t-stats/drop-top-N assume false independence and overstate significance.
   * QUEUE MODEL selectable: optimistic (queue fixed at post) | pessimistic (queue re-pads to
     live displayed size — models makers joining ahead / our deprioritization).
   * LATENCY pickoff: our cancels/re-quotes are delayed --latency-ms; during that stale window
     adverse flow can still hit the order we'd have pulled. This is the core maker risk.
   * Real Kalshi maker FEE schedule: ceil(coef·C·P·(1-P)) cents (default coef 0 = held-to-
     settlement near-zero maker fee; set --maker-fee-coef to stress).

MODEL (documented approximations — still v0 in spirit, calibrate before any capital):
   * Book reconstructed via the collector's Book class (snapshot + seq-gap-aware deltas; a
     `gap` event resets until the next snapshot). At L1 each `snapshot` is a fresh top-of-book.
   * Fair value = yes-mid = (best_yes_bid + best_yes_ask)/2, yes_ask = 1 - best_no_bid.
   * Policy is event-driven (re-quotes on every book change), not polled.
   * Fill trigger = trades AT our price on the side that lifts us (taker buying YES at our
     yes-ask; taker selling YES at our yes-bid). Queue consumed FIFO before our size.
   * Settlement P&L from a `settle` event; for real tapes use kalshi_public_tape --join-settles.
   * REMAINING TODO before capital: partial-queue refresh from cancels (not just trades);
     intra-market fill autocorrelation in the t-stat; depth>L1 once the Gate-3 WS tape exists.

USAGE:
  python3 prediction-markets/scripts/kalshi_maker_replay.py --tape data/l2/public_run1.jsonl \
      --queue-model pessimistic --latency-ms 250 --maker-fee-coef 0.0
  python3 prediction-markets/scripts/kalshi_maker_replay.py --selftest   # no data/creds needed

Stdlib-only. Reads JSONL from the collector. No orders, no network.
"""
from __future__ import annotations
import argparse, json, math, os, sys
from typing import cast

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from kalshi_l2_ws_collector import Book, _norm_price  # noqa: E402  (reuses collector book/delta logic)

CENT = 2


def _r(p):
    return round(p, CENT)


# ------------------------------------------------------------------ book → top of book
def best_yes_bid(bk: Book):
    return max(bk.yes) if bk.yes else None


def best_yes_ask(bk: Book):
    return _r(1 - max(bk.no)) if bk.no else None


def yes_mid(bk: Book):
    b, a = best_yes_bid(bk), best_yes_ask(bk)
    if b is None and a is None:
        return None
    if b is None:
        return a
    if a is None:
        return b
    return (b + a) / 2


def size_at_yes_ask(bk: Book, yes_price):
    return bk.no.get(_r(1 - yes_price), 0)


def size_at_yes_bid(bk: Book, yes_price):
    return bk.yes.get(_r(yes_price), 0)


def _displayed(bk: Book, kind, price):
    return size_at_yes_ask(bk, price) if kind == "yes_ask" else size_at_yes_bid(bk, price)


# ------------------------------------------------------------------ fee
def kalshi_fee(coef, price, count):
    """Kalshi-style fee: ceil(coef·C·P·(1-P)) cents → dollars. coef 0 = no maker fee."""
    if coef <= 0:
        return 0.0
    cents = round(coef * count * price * (1 - price) * 100, 9)  # round kills fp dust before ceil
    return math.ceil(cents) / 100


# ------------------------------------------------------------------ policy
class LongshotFadePolicy:
    """Fade the overpriced cheap longshot: rest a YES ASK just above mid when yes-mid is in
    [lo, hi). Single resting quote; event-driven re-quote."""

    def __init__(self, lo=0.01, hi=0.30, half_spread=0.01, order_size=100):
        self.lo, self.hi, self.half_spread, self.order_size = lo, hi, half_spread, order_size

    def desired(self, bk: Book):
        m = yes_mid(bk)
        if m is None or not (self.lo <= m < self.hi):
            return None
        price = _r(m + self.half_spread)
        if not (0 < price < 1):
            return None
        return ("yes_ask", price, self.order_size)


# ------------------------------------------------------------------ model + order
class Model:
    def __init__(self, queue="optimistic", latency_ms=0.0, fee_coef=0.0):
        assert queue in ("optimistic", "pessimistic")
        self.queue, self.latency_ms, self.fee_coef = queue, float(latency_ms), float(fee_coef)


class RestingOrder:
    __slots__ = ("kind", "price", "remaining", "queue_ahead", "stale_since")

    def __init__(self, kind, price, size, queue_ahead):
        self.kind, self.price = kind, price
        self.remaining = size
        self.queue_ahead = queue_ahead
        self.stale_since = None  # ts_ms when desired diverged from this order (latency clock)


def _ms(ev, idx):
    """Event time in ms. Real tapes: ts_recv (epoch s)·1000. Synthetic: ts_recv if given, else idx."""
    ts = ev.get("ts_recv")
    return (ts * 1000) if ts is not None else float(idx)


# ------------------------------------------------------------------ replay engine
def replay(events, policy: LongshotFadePolicy, model: Model | None = None):
    model = model or Model()
    books: dict[str, Book] = {}
    resting: dict[str, RestingOrder] = {}
    fills: list[dict] = []

    def place(tk, bk, kind, price, size):
        resting[tk] = RestingOrder(kind, price, size, _displayed(bk, kind, price))

    def manage(tk, ts_ms):
        bk = books.get(tk)
        if bk is None:
            return
        want = policy.desired(bk)
        cur = resting.get(tk)
        same = bool(cur and want and cur.kind == want[0] and _r(cur.price) == _r(want[1]))
        if same:
            assert cur is not None
            cur.stale_since = None
            if model.queue == "pessimistic":
                cur.queue_ahead = max(cur.queue_ahead, _displayed(bk, cur.kind, cur.price))
            return
        if cur is None:
            if want:
                place(tk, bk, *want)
            return
        # existing order no longer matches desired → latency-gated cancel/re-quote
        if cur.stale_since is None:
            cur.stale_since = ts_ms
        if ts_ms - cur.stale_since >= model.latency_ms:
            resting.pop(tk, None)
            if want:
                place(tk, bk, *want)

    for idx, ev in enumerate(events):
        t = ev.get("type")
        tk = ev.get("ticker")
        ts_ms = _ms(ev, idx)

        if t == "snapshot":
            books.setdefault(tk, Book()).apply_snapshot(ev["msg"], ev.get("sid"), ev.get("seq"))
            manage(tk, ts_ms)

        elif t == "delta":
            bk = books.setdefault(tk, Book())
            if bk.apply_delta(ev["msg"], ev.get("seq")):
                manage(tk, ts_ms)

        elif t == "gap":
            if tk in books:
                books[tk].reset()
            resting.pop(tk, None)

        elif t == "trade":
            manage(tk, ts_ms)  # process any latency-elapsed re-quote AS OF the trade time first
            o = resting.get(tk)
            if not o:
                continue
            msg = ev["msg"]
            tp = _norm_price(msg.get("yes_price", msg.get("price")))
            taker = msg.get("taker_side")
            cnt = msg.get("count") or 0
            if tp is None or _r(tp) != _r(o.price):
                continue
            lifts = (o.kind == "yes_ask" and taker == "yes") or (o.kind == "yes_bid" and taker == "no")
            if not lifts:
                continue
            consumed = min(cnt, o.queue_ahead)
            o.queue_ahead -= consumed
            cnt -= consumed
            if cnt > 0 and o.remaining > 0:
                f = min(cnt, o.remaining)
                o.remaining -= f
                fills.append({"ticker": tk, "kind": o.kind, "price": o.price, "qty": f})
                if o.remaining <= 0:
                    resting.pop(tk, None)

        elif t == "settle":
            settle_yes = 1.0 if ev.get("result") == "yes" else 0.0
            for f in fills:
                if f["ticker"] != tk or "pnl" in f:
                    continue
                per = (f["price"] - settle_yes) if f["kind"] == "yes_ask" else (settle_yes - f["price"])
                fee = kalshi_fee(model.fee_coef, f["price"], f["qty"])
                f["pnl"] = per * f["qty"] - fee
                f["fee"] = fee
                f["settle_yes"] = settle_yes

    return fills


# ------------------------------------------------------------------ reporting (per-MARKET)
def _tstat(xs):
    if len(xs) < 2:
        return None
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
    sd = math.sqrt(var)
    return round(mean / (sd / math.sqrt(len(xs))), 3) if sd > 0 else None


def report(fills, model: Model | None = None):
    model = model or Model()
    settled = [f for f in fills if "pnl" in f]
    out: dict[str, object] = {
        "model": {"queue": model.queue, "latency_ms": model.latency_ms, "fee_coef": model.fee_coef},
        "n_fills": len(fills), "n_settled_fills": len(settled),
    }
    if not settled:
        out["note"] = "no settled fills"
        return out

    # aggregate to per-market round-trips — the unit of independence for stats
    per_mkt: dict[str, dict] = {}
    for f in settled:
        d = per_mkt.setdefault(f["ticker"], {"net": 0.0, "qty": 0.0, "price": f["price"]})
        d["net"] += f["pnl"]
        d["qty"] += f["qty"]
    mkt_nets = [d["net"] for d in per_mkt.values()]
    contracts = sum(d["qty"] for d in per_mkt.values())
    net = sum(mkt_nets)

    out["n_markets"] = len(per_mkt)
    out["contracts"] = round(contracts, 2)
    out["net_pnl"] = round(net, 4)
    out["net_per_contract"] = round(net / contracts, 5) if contracts else None
    out["net_per_market_mean"] = round(net / len(per_mkt), 5)
    out["t_stat_per_market"] = _tstat(mkt_nets)               # honest unit
    out["t_stat_per_fill_NAIVE"] = _tstat([f["pnl"] for f in settled])  # shown only to expose inflation
    srt = sorted(mkt_nets, reverse=True)                      # drop-top-N over MARKETS
    for n in (1, 5):
        if len(srt) > n:
            out[f"net_drop_top_{n}_markets"] = round(sum(srt[n:]), 4)
    out["total_fees"] = round(sum(f.get("fee", 0.0) for f in settled), 4)

    buckets: dict[str, list] = {}
    for d in per_mkt.values():
        b = f"[{int(d['price']*20)/20:.2f},{int(d['price']*20)/20+0.05:.2f})"
        buckets.setdefault(b, []).append(d["net"])
    out["by_bucket_per_market"] = {b: {"n_markets": len(v), "net": round(sum(v), 4)}
                                   for b, v in sorted(buckets.items())}
    return out


# ================================================================== self-tests
def _snap(tk, seq, yes, no, ts=None):
    e = {"type": "snapshot", "ticker": tk, "sid": 1, "seq": seq, "msg": {"market_id": tk, "yes": yes, "no": no}}
    if ts is not None:
        e["ts_recv"] = ts
    return e


def _trade(tk, price, taker, count, ts=None):
    e = {"type": "trade", "ticker": tk, "msg": {"market_ticker": tk, "yes_price": price, "taker_side": taker, "count": count}}
    if ts is not None:
        e["ts_recv"] = ts
    return e


def _accounting_tape():
    """T1 win (queue gates first 50, fills 70, settle NO → +14.70); T2 adverse (fills 40,
    settle YES → -31.60); a gap on T1 before settle."""
    ev = []
    ev += [_snap("T1", 1, [[0.19, 300]], [[0.79, 50]]), _trade("T1", 0.21, "yes", 50),
           _trade("T1", 0.21, "yes", 70), {"type": "gap", "ticker": "T1", "sid": 1},
           {"type": "settle", "ticker": "T1", "result": "no"}]
    ev += [_snap("T2", 1, [[0.19, 300]], [[0.79, 10]]), _trade("T2", 0.21, "yes", 50),
           {"type": "settle", "ticker": "T2", "result": "yes"}]
    return ev


def _per_market_tape():
    """One market, TWO fills (30+30) settle NO → per-market = +12.60 from 2 fills.
    queue_ahead 10; trade 40 eats queue+fills 30, trade 30 fills 30."""
    return [_snap("M1", 1, [[0.19, 300]], [[0.79, 10]]),
            _trade("M1", 0.21, "yes", 40), _trade("M1", 0.21, "yes", 30),
            {"type": "settle", "ticker": "M1", "result": "no"}]


def _pessimistic_tape():
    """Same-price displayed size refreshes 50→200 (makers join ahead) before a 120-lot trade."""
    return [_snap("Q1", 1, [[0.19, 300]], [[0.79, 50]]),    # post ask@0.21, queue 50
            _snap("Q1", 2, [[0.19, 300]], [[0.79, 200]]),   # displayed refills to 200
            _trade("Q1", 0.21, "yes", 120),                 # consumes queue, then maybe us
            {"type": "settle", "ticker": "Q1", "result": "no"}]


def _latency_tape():
    """Fair value moves UP out of band (we WANT to cancel) at t=1000ms; adverse 50-lot lifts our
    stale ask@0.21 at t=1100ms; settle YES. latency<100ms → canceled in time (no fill);
    latency>100ms → picked off into a -39.50 loss."""
    return [_snap("L1", 1, [[0.19, 300]], [[0.79, 50]], ts=0.0),     # post ask@0.21, queue 50
            _snap("L1", 2, [[0.39, 300]], [[0.59, 50]], ts=1.0),     # mid→0.40, desired=None (cancel)
            _trade("L1", 0.21, "yes", 100, ts=1.1),                  # 100ms later: 50 queue + 50 us
            {"type": "settle", "ticker": "L1", "result": "yes"}]


def selftest():
    checks = []
    def chk(name, cond):
        checks.append((name, bool(cond)))

    # --- accounting (unchanged behaviour, default model) ---
    fills = replay(_accounting_tape(), LongshotFadePolicy())
    rep = report(fills)
    t1 = [f for f in fills if f["ticker"] == "T1"]
    t2 = [f for f in fills if f["ticker"] == "T2"]
    chk("acct: T1 one fill of 70 (queue gated 50)", len(t1) == 1 and t1[0]["qty"] == 70)
    chk("acct: T1 net +14.70 (settle NO)", t1 and abs(t1[0]["pnl"] - 14.70) < 1e-9)
    chk("acct: T2 net -31.60 (settle YES)", t2 and abs(t2[0]["pnl"] + 31.60) < 1e-9)
    chk("acct: total net -16.90", abs(cast(float, rep["net_pnl"]) + 16.90) < 1e-9)
    chk("acct: 2 markets, drop-top-1 = -31.60", rep["n_markets"] == 2
        and abs(cast(float, rep["net_drop_top_1_markets"]) + 31.60) < 1e-9)

    # --- per-market aggregation: 2 fills collapse to 1 market ---
    repm = report(replay(_per_market_tape(), LongshotFadePolicy()))
    chk("per-mkt: 2 fills → 1 market", repm["n_settled_fills"] == 2 and repm["n_markets"] == 1)
    chk("per-mkt: market net +12.60", abs(cast(float, repm["net_pnl"]) - 12.60) < 1e-9)
    chk("per-mkt: single-market t-stat is None (n=1)", repm["t_stat_per_market"] is None)

    # --- pessimistic queue suppresses the fill optimistic would take ---
    opt = replay(_pessimistic_tape(), LongshotFadePolicy(), Model(queue="optimistic"))
    pes = replay(_pessimistic_tape(), LongshotFadePolicy(), Model(queue="pessimistic"))
    chk("queue: optimistic fills 70 (queue stays 50)", len(opt) == 1 and opt[0]["qty"] == 70)
    chk("queue: pessimistic fills 0 (queue re-padded to 200)", len(pes) == 0)

    # --- latency pickoff ---
    fast = replay(_latency_tape(), LongshotFadePolicy(), Model(latency_ms=50))
    slow = replay(_latency_tape(), LongshotFadePolicy(), Model(latency_ms=500))
    chk("latency: fast cancel (50ms) avoids the fill", len(fast) == 0)
    chk("latency: slow cancel (500ms) is picked off, fills 50", len(slow) == 1 and slow[0]["qty"] == 50)
    chk("latency: pickoff books -39.50 (settle YES)", slow and abs(slow[0]["pnl"] + 39.50) < 1e-9)

    # --- fee schedule (ceil-to-cent) ---
    chk("fee: 0.07·100·0.5·0.5 = 1.75", abs(kalshi_fee(0.07, 0.5, 100) - 1.75) < 1e-9)
    chk("fee: ceil rounding 0.81291 → 0.82", abs(kalshi_fee(0.07, 0.21, 70) - 0.82) < 1e-9)
    chk("fee: coef 0 → free", kalshi_fee(0.0, 0.21, 70) == 0.0)

    print("accounting report →", json.dumps(rep, indent=2))
    print("\nassertions:")
    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and cond
    print("\n" + ("ALL PASS ✓ — model + accounting verified (synthetic; NOT an edge claim)"
                  if ok else "FAILURES ✗"))
    return 0 if ok else 1


# ------------------------------------------------------------------ main
def load_tape(path):
    ev = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                ev.append(json.loads(line))
    ev.sort(key=lambda e: e.get("rec", 0))
    return ev


def main():
    ap = argparse.ArgumentParser(description="Adverse-selection-aware Kalshi maker backtest (v1)")
    ap.add_argument("--tape", help="JSONL tape from the L2/L1 collector")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--lo", type=float, default=0.01)
    ap.add_argument("--hi", type=float, default=0.30, help="longshot band upper bound (yes-mid)")
    ap.add_argument("--half-spread", type=float, default=0.01)
    ap.add_argument("--order-size", type=int, default=100)
    ap.add_argument("--queue-model", choices=["optimistic", "pessimistic"], default="optimistic")
    ap.add_argument("--latency-ms", type=float, default=0.0)
    ap.add_argument("--maker-fee-coef", type=float, default=0.0)
    a = ap.parse_args()

    if a.selftest:
        sys.exit(selftest())
    if not a.tape:
        sys.exit("need --tape <file> or --selftest")

    pol = LongshotFadePolicy(a.lo, a.hi, a.half_spread, a.order_size)
    model = Model(a.queue_model, a.latency_ms, a.maker_fee_coef)
    print(json.dumps(report(replay(load_tape(a.tape), pol, model), model), indent=2))


if __name__ == "__main__":
    main()
