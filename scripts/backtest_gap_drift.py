#!/usr/bin/env python3
"""
backtest_gap_drift.py — does a catalyst gap DRIFT? (the "hotter edge" hypothesis)

backtest_signal.py killed the intraday absolute-pop signal (reversal at 1d). backtest_xsection.py
found a medium-term momentum edge (survivorship-caveated). This tests the third, most agentic-
friendly thesis: post-earnings-announcement drift (PEAD). A big OVERNIGHT GAP on a VOLUME SPIKE is a
keyless proxy for a real catalyst (earnings beat, guidance raise, contract, M&A). PEAD says
under-covered names UNDER-react to such catalysts and drift over the following days — strongest where
coverage is thin. That is exactly the breadth+drift edge an agent (read every filing across thousands
of names) could scale, and it is a MULTI-DAY edge, dodging the 1-day reversal that killed the pop signal.

We measure the drift from the CLOSE OF THE GAP DAY forward (textbook PEAD — conservative; it does not
even claim the gap-day open->close pop). Keyless Cboe daily OHLCV.

=== SIGNAL ===
  gap_pct  = open[D]/close[D-1] - 1   >= --gap%          (the catalyst jump)
  vol_mult = volume[D]/avg(vol[D-20:D]) >= --vol-mult    (a real catalyst comes with volume)

=== EDGE TEST (the core) ===
  forward H-day close-to-close return on GAP days vs ALL days, per universe, with a t-stat. If gap
  days drift ABOVE baseline with |t|>~2, the catalyst-drift edge is real for that universe.

=== HONESTY ===
  Both universes are today's survivors (optimistic); the mid-cap basket is ALSO recency-biased
  (mostly post-2020 IPOs through a bull-then-bear regime) — treat its absolute numbers with extra
  suspicion and read the large-cap control as the trustworthy comparison. No earnings calendar: the
  gap+volume filter is a PROXY for a catalyst, not a confirmed one (that confirmation is the agent's
  live job). Daily bars: entry is modeled at the gap day's close, not intraday.

Usage:
  python3 scripts/backtest_gap_drift.py                       # both universes, defaults
  python3 scripts/backtest_gap_drift.py --gap 7 --vol-mult 3 --hold 10
  python3 scripts/backtest_gap_drift.py --sweep               # gap x hold grid, both universes
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CACHE = REPO / "data" / "backtest" / "history"
CBOE_HIST = "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/{sym}.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

LARGE = [
    "AAPL", "MSFT", "NVDA", "AMD", "GOOGL", "META", "AMZN", "AVGO", "ORCL", "CRM",
    "ADBE", "INTC", "CSCO", "QCOM", "TXN", "NFLX", "DIS", "TSLA", "HD", "NKE",
    "SBUX", "MCD", "LOW", "TGT", "JPM", "BAC", "WFC", "GS", "MS", "C",
    "V", "MA", "AXP", "UNH", "JNJ", "PFE", "MRK", "ABBV", "LLY", "TMO",
    "XOM", "CVX", "CAT", "BA", "GE", "HON", "UPS", "DE", "PG", "KO",
    "PEP", "WMT", "COST", "T", "VZ", "CMCSA", "IBM", "GILD", "AMGN", "BKNG",
]
# Higher-beta, less-covered mid-caps. SURVIVORSHIP + RECENCY biased (mostly post-2020 IPOs) — its
# absolute numbers are the least trustworthy; it exists to show the inefficiency GRADIENT vs LARGE.
MIDCAP = [
    "PLTR", "RIVN", "SOFI", "CELH", "SMCI", "IONQ", "APP", "RKLB", "CVNA", "AFRM",
    "ENPH", "FSLR", "PLUG", "RUN", "UPST", "DKNG", "RBLX", "U", "COIN", "HOOD",
    "MARA", "RIOT", "CLSK", "SNAP", "PINS", "ROKU", "DDOG", "NET", "CRWD", "ZS",
    "SNOW", "MDB", "OKTA", "TWLO", "DOCU", "CHWY", "ETSY", "W", "DKS", "WING",
]


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


MIN_PRICE = 2.0          # below this = penny/junk; also filters reverse-split artifacts
JUNK_JUMP = 0.80         # an >80% overnight move on a cash equity = bad data / pre-listing shell


def load_bars(sym: str, refresh: bool = False) -> list[dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{sym}.json"
    if path.exists() and not refresh:
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    try:
        d = json.loads(_http_get(CBOE_HIST.format(sym=urllib.parse.quote(sym.upper()))))
        bars = [b for b in (d.get("data") or [])
                if b.get("close") and b.get("open") and b.get("volume")]
        bars.sort(key=lambda b: b["date"])
        path.write_text(json.dumps(bars))
        return bars
    except Exception as e:
        sys.stderr.write(f"[gap] {sym}: fetch failed: {e}\n")
        return []


def clean_bars(bars: list[dict]) -> list[dict]:
    """Strip Cboe's pre-listing junk: SPAC shells back-fill a ticker with ~$0.01 ghost bars years
    before the real IPO, producing 100,000%+ ghost 'returns'. Drop sub-$2 bars, then start the
    series AFTER the last >80% overnight jump (the boundary between ghost data and real trading)."""
    bars = [b for b in bars if b["close"] >= MIN_PRICE and b["open"] >= MIN_PRICE]
    last_glitch = 0
    for i in range(1, len(bars)):
        if abs(bars[i]["close"] / bars[i - 1]["close"] - 1) > JUNK_JUMP:
            last_glitch = i
    return bars[last_glitch:]


def collect(universe: list[str], gap_pct: float, vol_mult: float, hold: int,
            stop: float, cost_bps: float, refresh: bool) -> dict:
    """Return gap-day forward returns, all-day forward returns, and traded P&L for a universe."""
    gap_fwd, all_fwd, traded = [], [], []
    dropped = 0
    for sym in universe:
        bars = clean_bars(load_bars(sym, refresh))
        if len(bars) < 30 + hold:
            continue
        vols = [b["volume"] for b in bars]
        for i in range(21, len(bars) - hold):
            b, prev = bars[i], bars[i - 1]
            c0, cH = b["close"], bars[i + hold]["close"]
            fwd = cH / c0 - 1
            if abs(fwd) > 3.0:            # data-guard: >300% in `hold` days is a residual glitch
                dropped += 1
                continue
            all_fwd.append(fwd)                               # unconditional baseline
            gap = b["open"] / prev["close"] - 1
            avgv = sum(vols[i - 20:i]) / 20.0
            if avgv <= 0:
                continue
            if gap * 100 < gap_pct or b["volume"] / avgv < vol_mult:
                continue
            gap_fwd.append(fwd)                               # drift from gap-day close
            # Traded: enter at gap-day close, gap-aware stop, else exit at close[D+hold].
            entry = c0
            stop_px = entry * (1 - stop / 100.0)
            exit_ret = cH / entry - 1
            for k in range(1, hold + 1):
                bar = bars[i + k]
                if bar["open"] <= stop_px:
                    exit_ret = bar["open"] / entry - 1
                    break
                if bar["low"] <= stop_px:
                    exit_ret = stop_px / entry - 1
                    break
            traded.append(exit_ret - 2 * cost_bps / 10000.0)
    return {"gap_fwd": gap_fwd, "all_fwd": all_fwd, "traded": traded, "dropped": dropped}


def stats(xs: list[float]) -> tuple[float, float]:
    if not xs:
        return 0.0, 0.0
    m = sum(xs) / len(xs)
    if len(xs) < 2:
        return m, 0.0
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return m, math.sqrt(var)


def median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def edge_t(gap_fwd: list[float], baseline_mean: float) -> float:
    """t-stat of gap-day forward returns vs the unconditional baseline mean."""
    m, sd = stats(gap_fwd)
    if sd <= 0 or not gap_fwd:
        return float("nan")
    return (m - baseline_mean) / (sd / math.sqrt(len(gap_fwd)))


def pct(x: float) -> str:
    return f"{x*100:+.2f}%"


def report_universe(name: str, r: dict, hold: int, cost_bps: float) -> None:
    gm, _ = stats(r["gap_fwd"])
    am, _ = stats(r["all_fwd"])
    gmed, amed = median(r["gap_fwd"]), median(r["all_fwd"])
    t = edge_t(r["gap_fwd"], am)
    tr = r["traded"]
    tm, _ = stats(tr)
    tmed = median(tr)
    wins = sum(1 for x in tr if x > 0)
    wr = wins / len(tr) if tr else 0
    print(f"  [{name}]  gap events: {len(r['gap_fwd'])}   (dropped {r['dropped']} glitch bars)")
    print(f"    Fwd {hold}d mean   GAP: {pct(gm)}   ALL: {pct(am)}   edge: {pct(gm-am)}  t={t:+.2f}")
    print(f"    Fwd {hold}d median GAP: {pct(gmed)}   ALL: {pct(amed)}   edge: {pct(gmed-amed)}"
          "   <- robust")
    print(f"    Traded (net {2*cost_bps:.0f}bps, stop): mean {pct(tm)}  median {pct(tmed)}  "
          f"win {wr*100:.0f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap", type=float, default=5.0, help="min overnight gap %%")
    ap.add_argument("--vol-mult", type=float, default=2.0, help="min volume vs 20d avg")
    ap.add_argument("--hold", type=int, default=10, help="drift window, trading days")
    ap.add_argument("--stop", type=float, default=8.0, help="stop %% for the traded sim")
    ap.add_argument("--cost-bps", type=float, default=15.0, help="per side (wider for smaller names)")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    universes = [("LARGE", LARGE), ("MIDCAP", MIDCAP)]
    print(f"Loading history (cached if present)...", file=sys.stderr)

    if args.sweep:
        print("=" * 74)
        print("GAP-DRIFT SWEEP — fwd-return edge (gap days - all days) | t-stat, per universe")
        print(f"(vol-mult={args.vol_mult}x, survivorship+recency biased — read the GRADIENT)")
        print("=" * 74)
        for uname, u in universes:
            print(f"\n[{uname}]")
            print(f"  {'gap%':>5} {'hold':>5} | {'edge':>8} {'t':>6} {'n':>6}")
            print("  " + "-" * 40)
            for g in (5, 7, 10, 15):
                for h in (3, 5, 10, 20):
                    r = collect(u, g, args.vol_mult, h, args.stop, args.cost_bps, args.refresh)
                    gm, _ = stats(r["gap_fwd"])
                    am, _ = stats(r["all_fwd"])
                    t = edge_t(r["gap_fwd"], am)
                    print(f"  {g:>5} {h:>5} | {(gm-am)*100:>+7.2f}% {t:>6.2f} {len(r['gap_fwd']):>6}")
        print("\n" + "-" * 74)
        print("|t|>~2 = gap days drift significantly above baseline. Compare LARGE vs MIDCAP edge.")
        return 0

    w = 74
    print("=" * w)
    print("CATALYST GAP-DRIFT BACKTEST  (PEAD proxy: overnight gap + volume -> drift)")
    print("=" * w)
    print(f"Signal : gap >= {args.gap}% AND volume >= {args.vol_mult}x 20d-avg")
    print(f"Drift  : forward {args.hold}d from gap-day close | stop -{args.stop}% | "
          f"cost {args.cost_bps}bps/side")
    print("-" * w)
    for uname, u in universes:
        r = collect(u, args.gap, args.vol_mult, args.hold, args.stop, args.cost_bps, args.refresh)
        report_universe(uname, r, args.hold, args.cost_bps)
    print("=" * w)
    print("Compare LARGE vs MIDCAP edge: a bigger drift in the less-covered universe is the")
    print("inefficiency gradient PEAD predicts — and the corner an agent's breadth can exploit.")
    print("Caveat: gap+volume is a catalyst PROXY; confirming the catalyst is real is the agent's")
    print("live job. Survivorship/recency bias flatters MIDCAP — validate out-of-universe.")
    print("=" * w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
