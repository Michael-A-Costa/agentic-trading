#!/usr/bin/env python3
"""
kalshi_public_tape.py — forward, UNAUTH, harness-shaped L1 tape for the longshot-fade test.

WHY (the Gate-3-free path to a real Phase-A signal):
  The maker backtest (kalshi_maker_replay.py) needs forward book + trade data Kalshi doesn't
  serve historically. The tick-level WS collector needs credentials (= Gate 3). But a FIRST
  empirical go/no-go on longshot-fade does NOT need full depth: the quote rests at/near the
  top level in a thin longshot, so TOP-OF-BOOK + the PUBLIC trade tape is a usable
  approximation — and both are unauth public data (within the subtree's "public read-only
  until Gate 3" ceiling). This collector produces exactly the tape the harness already eats.

WHAT IT EMITS (one append-only JSONL, harness event schema; prices in DOLLARS 0-1):
  * `snapshot` per polled market per round — top-of-book as single-level yes/no ladders:
      yes = [[yes_bid, yes_bid_size]],  no = [[no_bid, yes_ask_size]]
    (no_bid = 1 - yes_ask; yes_ask_size is the queue resting at our yes-ask price). Each poll
    is a fresh snapshot → the harness Book resets to current top-of-book (no deltas at L1).
  * `trade` per NEW public trade (deduped by trade_id): yes_price, count, taker_side.
  * `settle` when a market is observed resolved (status finalized/settled + result yes|no).
    Markets that resolve after the run ends: backfill with `--join-settles <tape>`.

FOCUS: only markets with yes-mid < --band-hi + buffer are taped (that's the longshot-fade
  universe) — bounds API calls + storage, and is exactly what we backtest.

CURRENT KALSHI SCHEMA (verified live 2026-06-16): *_dollars (str) + *_fp (str) fields;
  trades carry yes_price_dollars / count_fp / taker_side.

USAGE:
  # build/refresh the liquid-series cache first (one-time):
  python3 prediction-markets/scripts/kalshi_pull.py --discover
  # collect forward (top-15 liquid series, every 15s, for 6h) into a tape:
  python3 prediction-markets/scripts/kalshi_public_tape.py --top 15 --interval 15 \
      --duration 21600 --out prediction-markets/data/l2/public_run1.jsonl
  # after markets resolve, backfill settlement outcomes:
  python3 prediction-markets/scripts/kalshi_public_tape.py --join-settles prediction-markets/data/l2/public_run1.jsonl
  # then score it:
  python3 prediction-markets/scripts/kalshi_maker_replay.py --tape prediction-markets/data/l2/public_run1.jsonl

Read-only, public data, stdlib-only (urllib via kalshi_pull). Ctrl-C keeps the partial tape.
"""
from __future__ import annotations
import argparse, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from kalshi_pull import BASE, CACHE, _get, _f, _is_parlay  # noqa: E402


def _load_target_series(top, series_arg):
    if series_arg:
        return [s.strip() for s in series_arg.split(",") if s.strip()]
    if not os.path.exists(CACHE):
        sys.exit("no discovery cache — run `kalshi_pull.py --discover` first, or pass --series")
    with open(CACHE) as f:
        return [r["series"] for r in json.load(f)[:top]]


def _yes_mid(m):
    b, a = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
    if b is None and a is None:
        return None
    if b is None:
        return a
    if a is None:
        return b
    return (b + a) / 2


class TapeWriter:
    def __init__(self, path, start_rec=0):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._f = open(path, "a")
        self._i = start_rec

    def write(self, rec):
        self._f.write(json.dumps({"rec": self._i, "ts_recv": time.time(), **rec}) + "\n")
        self._f.flush()
        self._i += 1

    @property
    def rec(self):
        return self._i

    def close(self):
        self._f.close()


def _emit_snapshot(w, m, seq):
    tk = m["ticker"]
    yb, ya = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
    ybs = _f(m.get("yes_bid_size_fp"), 0) or 0
    yas = _f(m.get("yes_ask_size_fp"), 0) or 0  # size at the yes-ask = no-bid queue
    yes = [[yb, ybs]] if (yb is not None and ybs > 0) else []
    no = [[round(1 - ya, 4), yas]] if (ya is not None and yas > 0) else []
    w.write({"type": "snapshot", "ticker": tk, "sid": 0, "seq": seq,
             "msg": {"market_id": tk, "yes": yes, "no": no}})


def _emit_trades(w, tk, seen, limit=100):
    d = _get(f"{BASE}/markets/trades?ticker={tk}&limit={limit}")
    n = 0
    for tr in reversed(d.get("trades") or []):  # oldest-first into the tape
        tid = tr.get("trade_id")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        yp = _f(tr.get("yes_price_dollars"))
        cnt = _f(tr.get("count_fp"), 0) or 0
        ts = tr.get("taker_side")
        if yp is None or cnt <= 0 or ts not in ("yes", "no"):
            continue
        w.write({"type": "trade", "ticker": tk,
                 "msg": {"market_ticker": tk, "yes_price": yp, "taker_side": ts, "count": cnt}})
        n += 1
    return n


def _settle_result(m):
    status = (m.get("status") or "").lower()
    res = (m.get("result") or "").lower()
    if status in ("finalized", "settled", "determined") and res in ("yes", "no"):
        return res
    return None


def collect(series_list, interval, duration, out_path, band_hi, buffer=0.05):
    w = TapeWriter(out_path)
    seen_trades = set()
    settled = set()
    seq = 0
    deadline = time.time() + duration if duration else None
    rounds = 0
    try:
        while deadline is None or time.time() < deadline:
            rounds += 1
            taped, trades = 0, 0
            for series in series_list:
                d = _get(f"{BASE}/markets?limit=200&status=open&series_ticker={series}")
                if "_err" in d:
                    continue
                for m in d.get("markets", []):
                    tk = m.get("ticker")
                    if not tk or _is_parlay(tk):
                        continue
                    mid = _yes_mid(m)
                    if mid is None or mid >= band_hi + buffer:
                        continue  # outside the longshot universe
                    seq += 1
                    _emit_snapshot(w, m, seq)
                    taped += 1
                    trades += _emit_trades(w, tk, seen_trades)
                    res = _settle_result(m)
                    if res and tk not in settled:
                        settled.add(tk)
                        w.write({"type": "settle", "ticker": tk, "result": res})
            print(f"[round {rounds}] taped {taped} longshot mkts, {trades} new trades, "
                  f"{len(settled)} settled, rec={w.rec}", file=sys.stderr)
            if deadline and time.time() + interval > deadline:
                break
            time.sleep(interval)
    finally:
        w.close()
    print(f"done: {rounds} rounds, {w.rec} records -> {out_path}", file=sys.stderr)


def join_settles(tape_path):
    """Backfill `settle` events for every ticker in a tape that resolved after collection."""
    tickers, already = set(), set()
    with open(tape_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            if e.get("ticker"):
                tickers.add(e["ticker"])
            if e.get("type") == "settle":
                already.add(e["ticker"])
    todo = sorted(tickers - already)
    print(f"{len(tickers)} tickers, {len(already)} already settled, querying {len(todo)} ...",
          file=sys.stderr)
    w = TapeWriter(tape_path, start_rec=_max_rec(tape_path) + 1)
    added = 0
    for tk in todo:
        d = _get(f"{BASE}/markets/{tk}")
        m = d.get("market") or d
        res = _settle_result(m)
        if res:
            w.write({"type": "settle", "ticker": tk, "result": res})
            added += 1
    w.close()
    print(f"appended {added} settle events", file=sys.stderr)


def _max_rec(path):
    mx = -1
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                mx = max(mx, json.loads(line).get("rec", -1))
    return mx


def main():
    ap = argparse.ArgumentParser(description="Kalshi public forward L1 tape (unauth, longshot band)")
    ap.add_argument("--series", help="comma list (else top-N from discovery cache)")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--interval", type=float, default=15.0, help="seconds between rounds")
    ap.add_argument("--duration", type=int, default=0, help="seconds; 0 = until Ctrl-C")
    ap.add_argument("--band-hi", type=float, default=0.30, help="longshot yes-mid upper bound")
    ap.add_argument("--out", default=os.path.join(HERE, "..", "data", "l2", "public_run.jsonl"))
    ap.add_argument("--join-settles", metavar="TAPE", help="backfill settlement outcomes into TAPE and exit")
    a = ap.parse_args()

    if a.__dict__["join_settles"]:
        join_settles(a.__dict__["join_settles"])
        return
    collect(_load_target_series(a.top, a.series), a.interval, a.duration, a.out, a.band_hi)


if __name__ == "__main__":
    main()
