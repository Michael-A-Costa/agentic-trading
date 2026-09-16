#!/usr/bin/env python3
"""
kalshi_l2_ws_collector.py — forward tick-level L2 recorder (v0 SKELETON, AUTHED WS).

  ⚠️  GATE-3 PENDING — DEMO/REFERENCE ONLY. DO NOT RUN WITH LIVE CREDENTIALS.  ⚠️
  The WebSocket `orderbook_delta` channel requires a Kalshi API key + RSA private key
  (a credential, not public data). The subtree rule (docs/CLAUDE.md) caps us at "public
  read-only market data ... until Gate 3," and Gate 3 requires a separate secrets store +
  kill switch. This file is the *worked reference* for that parked step (roadmap §1), to be
  wired in only WHEN/IF the owner clears Gate 3. Until then, use the unauth top-of-book
  poller `kalshi_l2_collector.py`.

WHY THIS EXISTS:
  Roadmap §1 needs the full book + fill tape collected FORWARD (Kalshi serves no historical
  L2) before the adverse-selection maker backtest can run. The unauth poller captures only
  top-of-book on a REST timer; this captures tick-level depth via `orderbook_delta`. Ported
  (translated) from the reference impl in alsk1992/CloddsBot `src/feeds/kalshi/index.ts` +
  `src/utils/kalshi-auth.ts` — see docs/cloddsbot-collector-review.md.

WHAT IT DOES (recorder, not a live feed):
  Subscribes to `orderbook_delta` (snapshot + deltas) for the target series' open markets,
  maintains an in-memory book per ticker (price->size, yes/no), and writes an append-only
  JSONL tape of EVERY event (snapshot/delta/trade) with a local recv timestamp + a monotonic
  record index. On a sequence GAP it (a) writes an explicit `{"type":"gap"}` marker, (b) clears
  state, (c) re-subscribes to pull a fresh snapshot — so the downstream backtest never applies
  deltas across a discontinuity.

NON-STDLIB DEPS (first in this subtree — flagged in the review):
  pip install websockets cryptography      # asyncio WS client + RSA-PSS signing

AUTH (Kalshi RSA-PSS, mirrors kalshi-auth.ts):
  sign( f"{ts_ms}{METHOD}{path}" ) with PSS(MGF1(SHA256), salt_len=DIGEST), base64 ->
  headers KALSHI-ACCESS-KEY / KALSHI-ACCESS-TIMESTAMP / KALSHI-ACCESS-SIGNATURE.
  Validate the WS message envelope against current Kalshi docs before trusting (the TS
  reference carried legacy-format fallbacks).

USAGE (once Gate-3 cleared; demo only):
  KALSHI_API_KEY_ID=... KALSHI_PRIVATE_KEY_PATH=key.pem \
    python3 prediction-markets/scripts/kalshi_l2_ws_collector.py \
      --series KXHIGHNY,KXWTI --duration 3600 --out prediction-markets/data/l2/ws_run1.jsonl

Read-only data flow (no orders). Ctrl-C to stop early (partial JSONL is kept).
"""
from __future__ import annotations
import argparse, asyncio, base64, json, os, sys, time
from urllib.parse import urlparse

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Reuse the liquid-series cache + parlay filter from the existing tooling.
from kalshi_pull import CACHE, _is_parlay  # noqa: E402


# ----------------------------------------------------------------------------- auth
def _load_private_key():
    """Load the RSA private key PEM. Cryptography import is local so the module can be
    inspected/imported without the dep until Gate 3."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    pem_inline = os.environ.get("KALSHI_PRIVATE_KEY")
    if pem_inline:
        pem = pem_inline.encode()
    elif pem_path:
        with open(pem_path, "rb") as f:
            pem = f.read()
    else:
        sys.exit("no key: set KALSHI_PRIVATE_KEY_PATH or KALSHI_PRIVATE_KEY (Gate-3 secret)")
    return load_pem_private_key(pem, password=None)


def _sign(priv, message: str) -> str:
    """RSA-PSS / SHA-256 / salt_len=digest, base64 — mirrors kalshi-auth.ts buildKalshiSignature."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    sig = priv.sign(
        message.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


def _auth_headers(priv, key_id: str, method: str, url: str) -> dict:
    ts = str(int(time.time() * 1000))
    path = urlparse(url).path
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": _sign(priv, f"{ts}{method.upper()}{path}"),
    }


# ----------------------------------------------------------------------------- helpers
def _norm_price(v):
    """Kalshi prices: (0,1) already decimal; [1,100] are cents -> /100. Mirrors normalizePrice."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if 0 < x < 1:
        return x
    if 1 <= x <= 100:
        return x / 100
    return None


def _resolve_open_tickers(series_list):
    """series -> concrete open-market tickers. STUB until Gate 3.
    TODO(Gate-3): for each series, authed `GET /markets?status=open&series_ticker=...`
    (reuse kalshi_pull._get), drop parlays via _is_parlay, return the tickers. Left a
    deliberate stub so the authed-REST path is an explicit secrets decision, not a default."""
    _ = (series_list, _is_parlay)  # inputs reserved for the authed resolution wired at Gate 3
    return []  # no markets resolved pre-Gate-3


def _load_target_series(top, series_arg):
    if series_arg:
        return [s.strip() for s in series_arg.split(",") if s.strip()]
    if not os.path.exists(CACHE):
        sys.exit("no discovery cache — run `kalshi_pull.py --discover` first, or pass --series")
    with open(CACHE) as f:
        return [r["series"] for r in json.load(f)[:top]]


class Tape:
    """Append-only JSONL writer: one line per event, with recv ts + monotonic record index."""
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._f = open(path, "a")
        self._i = 0

    def write(self, rec: dict):
        rec = {"rec": self._i, "ts_recv": time.time(), **rec}
        self._f.write(json.dumps(rec) + "\n")
        self._f.flush()
        self._i += 1

    def close(self):
        self._f.close()


class Book:
    """In-memory book per ticker (price->size, yes/no) with seq-gap detection.
    Mirrors the orderbookState + lastSeqBySid logic in feeds/kalshi/index.ts."""
    def __init__(self):
        self.yes: dict[float, float] = {}
        self.no: dict[float, float] = {}
        self.sid: int | None = None
        self.last_seq: int | None = None

    def reset(self):
        self.yes.clear()
        self.no.clear()
        self.last_seq = None

    def apply_snapshot(self, msg, sid, seq):
        self.reset()
        self.sid = sid
        for px, qty in (msg.get("yes") or []):
            p = _norm_price(px)
            if p is not None and qty > 0:
                self.yes[p] = qty
        for px, qty in (msg.get("no") or []):
            p = _norm_price(px)
            if p is not None and qty > 0:
                self.no[p] = qty
        self.last_seq = seq

    def apply_delta(self, msg, seq) -> bool:
        """Returns False on a sequence gap (caller must re-snapshot)."""
        if self.last_seq is not None and seq != self.last_seq + 1:
            return False
        side = msg.get("side")
        p = _norm_price(msg.get("price"))
        delta = msg.get("delta") if isinstance(msg.get("delta"), (int, float)) else 0
        if side in ("yes", "no") and p is not None:
            book = self.yes if side == "yes" else self.no
            book[p] = book.get(p, 0) + delta
            if book[p] <= 0:
                book.pop(p, None)
        self.last_seq = seq
        return True


# ----------------------------------------------------------------------------- collector
async def run(series_list, duration, out_path):
    import websockets  # local import: keep module importable without the dep pre-Gate-3

    priv = _load_private_key()
    key_id = os.environ.get("KALSHI_API_KEY_ID") or sys.exit("set KALSHI_API_KEY_ID (Gate-3 secret)")
    tape = Tape(out_path)
    books: dict[str, Book] = {}
    req_id = [1]
    deadline = time.time() + duration if duration else None

    tickers = _resolve_open_tickers(series_list)  # STUB pre-Gate-3 (see _resolve_open_tickers)

    async def subscribe(ws, ticker, channels):
        await ws.send(json.dumps({
            "id": req_id[0], "cmd": "subscribe",
            "params": {"channels": channels, "market_ticker": ticker},
        }))
        req_id[0] += 1

    backoff = 1.0
    while deadline is None or time.time() < deadline:
        try:
            headers = _auth_headers(priv, key_id, "GET", WS_URL)
            async with websockets.connect(WS_URL, additional_headers=headers) as ws:
                backoff = 1.0
                for tk in tickers:
                    books.setdefault(tk, Book())
                    await subscribe(ws, tk, ["orderbook_delta", "trade"])
                async for raw in ws:
                    if deadline and time.time() >= deadline:
                        break
                    m = json.loads(raw)
                    mtype = m.get("type")
                    body = m.get("msg") or m.get("data") or {}
                    sid, seq = m.get("sid"), m.get("seq")
                    tk = body.get("market_id") or body.get("market_ticker")

                    if mtype == "orderbook_snapshot" and tk:
                        books.setdefault(tk, Book()).apply_snapshot(body, sid, seq)
                        tape.write({"type": "snapshot", "ticker": tk, "sid": sid, "seq": seq, "msg": body})
                    elif mtype == "orderbook_delta" and tk:
                        bk = books.setdefault(tk, Book())
                        if bk.apply_delta(body, seq):
                            tape.write({"type": "delta", "ticker": tk, "sid": sid, "seq": seq, "msg": body})
                        else:
                            # GAP: record discontinuity, reset, re-snapshot (mirrors TS recovery).
                            tape.write({"type": "gap", "ticker": tk, "sid": sid,
                                        "expected": (bk.last_seq + 1) if bk.last_seq is not None else None,
                                        "got": seq})
                            bk.reset()
                            await ws.send(json.dumps({"id": req_id[0], "cmd": "unsubscribe",
                                                      "params": {"channels": ["orderbook_delta"], "market_ticker": tk}}))
                            req_id[0] += 1
                            await asyncio.sleep(0.1)
                            await subscribe(ws, tk, ["orderbook_delta"])
                    elif mtype == "trade" and tk:
                        tape.write({"type": "trade", "ticker": tk, "msg": body})
                    elif mtype == "error":
                        tape.write({"type": "error", "msg": body})
        except Exception as e:  # reconnect with exp backoff + jitter (mirrors scheduleWsReconnect)
            if deadline and time.time() >= deadline:
                break
            jitter = (hash(str(req_id[0])) % 1000) / 1000.0  # no Math.random; vary by req counter
            wait = min(30.0, backoff + jitter)
            tape.write({"type": "reconnect", "error": repr(e), "wait_s": round(wait, 2)})
            await asyncio.sleep(wait)
            backoff = min(30.0, backoff * 2)

    tape.close()


def main():
    ap = argparse.ArgumentParser(description="Kalshi authed WS L2 recorder (Gate-3 pending; demo only)")
    ap.add_argument("--series", help="comma list of series tickers (else top-N from discovery cache)")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--duration", type=int, default=0, help="seconds; 0 = until Ctrl-C")
    ap.add_argument("--out", default=os.path.join(HERE, "..", "data", "l2", "ws_run.jsonl"))
    ap.add_argument("--i-have-cleared-gate-3", action="store_true",
                    help="required ack: this uses live Kalshi credentials (see docs/CLAUDE.md Gate 3)")
    a = ap.parse_args()
    if not a.__dict__["i_have_cleared_gate_3"]:
        sys.exit("REFUSING: authed WS uses live credentials = Gate-3. Re-run with "
                 "--i-have-cleared-gate-3 only after the owner clears it. See "
                 "docs/cloddsbot-collector-review.md and docs/CLAUDE.md.")
    series_list = _load_target_series(a.top, a.series)
    try:
        asyncio.run(run(series_list, a.duration, a.out))
    except KeyboardInterrupt:
        print("\nstopped — partial tape kept", file=sys.stderr)


if __name__ == "__main__":
    main()
