#!/usr/bin/env python3
"""
bakeoff_crypto.py — daily-bar bake-off of simple long-or-cash crypto strategies, net of the
fees we would actually pay (Robinhood spread vs Coinbase Advanced vs a spot-ETF proxy).

Why this exists: before putting any money into a crypto sleeve we want to know whether a trend
filter / pullback rule / Donchian ensemble beats plain BTC buy & hold AFTER costs, and — the
owner's specific worry — what historically happened when a rule bought after a big rally
(BTC is ~15% above its 50d SMA as this is written). Robinhood's ~1.9% quoted buy/sell gap is
charged as ~0.95% per side, which can eat a high-turnover rule alive; the ETF proxy shows the
same signal at near-zero friction. The answer should come from numbers, not vibes.

Mechanics (no lookahead): every signal is computed on the day-t UTC close and traded at the
day t+1 open. Costs are charged per unit of turnover (|target - drifted holding|, per side).
Everything is long-or-cash, no leverage, cash earns 0 (Sharpe uses rf=0).

=== HONEST READING ===
- In-sample: every rule here is a well-known one picked AFTER a decade of crypto trending hard.
  The walk-forward block (params chosen on <=2021, scored on 2022+) is the only out-of-sample view.
- Survivorship: the "top coins" universe (BTC ETH SOL XRP DOGE ADA LINK LTC BCH AVAX DOT) is
  chosen with 2026 hindsight — every coin in it survived. Real-time you would also have held
  the ones that died. The Donchian ensemble is flattered by that.
- Short history: ~11 years of BTC, a handful of full cycles. A calendar year is one data point.
- Coinbase listing dates, not coin launch dates: the ensemble universe is BTC/ETH/LTC(/BCH)
  until 2019-2021 when the others list.

Usage:
  python3 scripts/bakeoff_crypto.py                     # fetch/refresh cache, run, write report
  python3 scripts/bakeoff_crypto.py --no-fetch          # cache only
  python3 scripts/bakeoff_crypto.py --stops --no-fetch  # stop-loss study on the BTC/ETH sleeve
  python3 scripts/bakeoff_crypto.py --rh-cost 0.0095 --cb-fee 0.006 --cb-slip 0.0002 \
      --etf-cost 0.0003 --etf-drag 0.0025 --ext 0.15
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "data" / "bakeoff"
CACHE = OUT_DIR / "crypto"
CB_URL = ("https://api.exchange.coinbase.com/products/{pair}/candles"
          "?granularity=86400&start={start}&end={end}")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) agentic-trading-bakeoff/1.0"
EARLIEST = dt.date(2015, 1, 1)
ANN = 365  # crypto trades every day

UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "LTC", "BCH", "AVAX", "DOT"]

PERIODS = [
    ("Full", None, None),
    ("2018-19", "2018-01-01", "2019-12-31"),
    ("2020-21", "2020-01-01", "2021-12-31"),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023-24", "2023-01-01", "2024-12-31"),
    ("2025-26/09", "2025-01-01", None),
]
IS_END = "2021-12-31"
OOS_START = "2022-01-01"


# --------------------------------------------------------------------------- data

def _get(url: str):
    for attempt in range(5):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429:
                time.sleep(2 + 2 * attempt)
                continue
            raise
        except urllib.error.URLError:
            time.sleep(2 + 2 * attempt)
    raise RuntimeError(f"giving up on {url}")


def _fetch_range(pair: str, start: dt.date, end: dt.date) -> list:
    """Candles with start <= day < end, walking backward in 299-day windows."""
    rows, win_end = [], end
    while win_end > start:
        win_start = max(start, win_end - dt.timedelta(days=299))
        url = CB_URL.format(pair=pair, start=f"{win_start}T00:00:00Z",
                            end=f"{win_end - dt.timedelta(days=1)}T00:00:00Z")
        data = _get(url)
        time.sleep(0.35)
        if data is None:
            return rows if rows else None
        rows.extend(data)
        win_end = win_start
    return rows


def load_pair(pair: str, fetch: bool) -> pd.DataFrame | None:
    """Daily OHLCV for a Coinbase pair, cached as CSV; the current partial UTC day is dropped."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{pair}.csv"
    today = dt.datetime.now(dt.timezone.utc).date()
    df = None
    if path.exists():
        df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
    if fetch:
        start = EARLIEST if df is None else (df.index[-1].date() + dt.timedelta(days=1))
        if start < today:
            rows = _fetch_range(pair, start, today)
            if rows is None and df is None:
                return None
            if rows:
                new = pd.DataFrame(rows, columns=["t", "low", "high", "open", "close", "volume"])
                new["date"] = pd.to_datetime(new["t"], unit="s").dt.normalize()
                new = new.drop(columns="t").set_index("date")
                df = new if df is None else pd.concat([df, new])
                df = df[~df.index.duplicated(keep="last")].sort_index()
                df = df[df.index < pd.Timestamp(today)]
                df.to_csv(path)
    if df is None or df.empty:
        return None
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def build_panel(fetch: bool):
    frames, info = {}, {}
    for coin in UNIVERSE:
        pair = f"{coin}-USD"
        df = load_pair(pair, fetch)
        if df is None:
            info[coin] = None
            print(f"  {pair}: no Coinbase history — skipped", file=sys.stderr)
            continue
        full = pd.date_range(df.index[0], df.index[-1], freq="D")
        missing = len(full) - len(df)
        frames[coin] = df
        # longest run of missing days (e.g. a delisting / trading halt)
        idx = df.index.to_series().diff().dt.days.fillna(1)
        info[coin] = dict(first=df.index[0].date(), last=df.index[-1].date(), rows=len(df),
                          missing=missing, longest_gap=int(idx.max() - 1))
        print(f"  {pair}: {df.index[0].date()} -> {df.index[-1].date()} ({len(df)} rows)",
              file=sys.stderr)
    dates = pd.date_range(min(f.index[0] for f in frames.values()),
                          max(f.index[-1] for f in frames.values()), freq="D")
    panel = {}
    for fld in ["open", "high", "low", "close"]:
        m = pd.DataFrame({c: f[fld] for c, f in frames.items()}).reindex(dates)
        # bridge short data holes only; a long gap (halt/delisting) stays NaN -> ineligible
        panel[fld] = m.ffill(limit=3)
    # Exchange-specific flash-crash prints (BTC 2017-04-15 low ~$0, GDAX ETH 2017-06-21 $0.10)
    # would "fill" stops no ETF could ever have seen; a low under half the candle body is
    # replaced by the body bottom. Real crash days (lows ~0.67-0.75x the body) are untouched.
    body = np.minimum(panel["open"], panel["close"])
    panel["low"] = panel["low"].mask(panel["low"] < 0.5 * body, body)
    return panel, info


# --------------------------------------------------------------------------- indicators

def sma(s, n):
    return s.rolling(n, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn)


def ann_vol(close: pd.Series, n: int = 30) -> pd.Series:
    return close.pct_change(fill_method=None).rolling(n, min_periods=n).std() * math.sqrt(ANN)


# --------------------------------------------------------------------------- strategies
# Each returns (target, rebal): target = weights decided at day-t close (DataFrame dates x coins),
# rebal = bool Series, True on decision days (trade happens next open).

def _single(panel, coin, w: pd.Series):
    t = pd.DataFrame(0.0, index=panel["close"].index, columns=panel["close"].columns)
    t[coin] = w.fillna(0.0)
    return t, pd.Series(True, index=t.index)


def s_buyhold(panel, coin):
    c = panel["close"][coin]
    return _single(panel, coin, c.notna().astype(float))


def s_sma_filter(panel, coin, n=50):
    c = panel["close"][coin]
    return _single(panel, coin, (c > sma(c, n)).astype(float))


def s_cross(panel, coin, fast=20, slow=100):
    c = panel["close"][coin]
    return _single(panel, coin, (sma(c, fast) > sma(c, slow)).astype(float))


def s_pullback(panel, coin, x=0.03, trend="px200", exit_rule="px200"):
    """Enter only on a pullback inside an uptrend; stay in until the exit rule fires.

    trend:  px200 = close > SMA200;  gc = SMA50 > SMA200
    entry:  close <= SMA50*(1+x)  OR  RSI14 < 40
    exit:   px200 = close < SMA200;  px50 = close < SMA50;  dc = SMA50 < SMA200 (death cross)
    """
    c = panel["close"][coin]
    s50, s200, r = sma(c, 50), sma(c, 200), rsi(c)
    up = (c > s200) if trend == "px200" else (s50 > s200)
    entry = (up & ((c <= s50 * (1 + x)) | (r < 40))).to_numpy()
    ex = {"px200": c < s200, "px50": c < s50, "dc": s50 < s200}[exit_rule].to_numpy()
    valid = s200.notna().to_numpy()
    w = np.zeros(len(c))
    pos = 0.0
    for i in range(len(c)):
        if not valid[i]:
            pos = 0.0
        elif pos == 0.0 and entry[i]:
            pos = 1.0
        elif pos == 1.0 and ex[i]:
            pos = 0.0
        w[i] = pos
    return _single(panel, coin, pd.Series(w, index=c.index))


def s_voltarget(panel, coin, target=0.50, n=50):
    """SMA-n filter, exposure = min(1, target_vol / 30d realized vol); resized weekly (Sunday
    close) or immediately when the filter flips."""
    c = panel["close"][coin]
    on = (c > sma(c, n))
    w = (target / ann_vol(c)).clip(upper=1.0).where(on, 0.0).fillna(0.0)
    t = pd.DataFrame(0.0, index=c.index, columns=panel["close"].columns)
    t[coin] = w
    flip = on.astype(int).diff().fillna(0) != 0
    rebal = pd.Series(c.index.dayofweek == 6, index=c.index) | flip
    return t, rebal


def s_donchian(panel, lookbacks=(20, 50, 100), full_invest=False):
    """Concretum-style trend ensemble: per coin, average of Donchian breakout states over the
    lookbacks (long on close > prior-N-day high, flat on close < prior-N/2-day low), each coin's
    slice sized by inverse 30d vol. Weekly (Sunday close) rebalance; cash when nothing is long.

    full_invest=False: slice_i = signal_i * (1/vol_i) / sum_j(1/vol_j) over ELIGIBLE coins, so the
      book is only 100% long when every coin is at full signal (risk-parity slices).
    full_invest=True:  weights renormalised over the coins with signal>0 (always 100% if any).
    """
    C, H, L = panel["close"], panel["high"], panel["low"]
    sig = pd.DataFrame(0.0, index=C.index, columns=C.columns)
    for coin in C.columns:
        c, h, lo = C[coin].to_numpy(), H[coin], L[coin]
        states = []
        for n in lookbacks:
            hi_n = h.shift(1).rolling(n, min_periods=n).max().to_numpy()
            lo_n = lo.shift(1).rolling(max(n // 2, 1), min_periods=max(n // 2, 1)).min().to_numpy()
            st = np.zeros(len(c))
            pos = 0.0
            for i in range(len(c)):
                if np.isnan(c[i]) or np.isnan(hi_n[i]):
                    pos = 0.0
                elif pos == 0.0 and c[i] > hi_n[i]:
                    pos = 1.0
                elif pos == 1.0 and c[i] < lo_n[i]:
                    pos = 0.0
                st[i] = pos
            states.append(st)
        sig[coin] = np.mean(states, axis=0)
    vol = C.apply(ann_vol)
    inv = (1.0 / vol).where(vol > 0)
    eligible = inv.notna() & C.notna()
    inv = inv.where(eligible, 0.0)
    if full_invest:
        raw = sig * inv
        w = raw.div(raw.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    else:
        w = (sig * inv).div(inv.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    rebal = pd.Series(C.index.dayofweek == 6, index=C.index)
    return w, rebal, sig


# --------------------------------------------------------------------------- engine

def simulate(panel, target: pd.DataFrame, rebal: pd.Series, cost: float, drag: float = 0.0):
    """Trade to `target` at the open after each decision day. Returns daily equity, gross
    exposure and turnover, all indexed like the panel."""
    O = panel["open"][target.columns].to_numpy()
    C = panel["close"][target.columns].to_numpy()
    T, A = C.shape
    gap = np.zeros((T, A))
    gap[1:] = O[1:] / C[:-1] - 1
    intr = C / O - 1
    gap = np.nan_to_num(gap)
    intr = np.nan_to_num(intr)
    W = target.to_numpy()
    R = rebal.to_numpy()
    eq, ex, to = np.ones(T), np.zeros(T), np.zeros(T)
    h, e = np.zeros(A), 1.0
    dd = drag / ANN
    for d in range(1, T):
        g = gap[d]
        p = h @ g
        if h.any():
            e *= 1 + p
            h = h * (1 + g) / (1 + p)
        if R[d - 1]:
            tw = W[d - 1]
            # a coin with no price at the trade open can't be bought
            tw = np.where(np.isnan(O[d]), np.minimum(tw, h), tw)
            turn = np.abs(tw - h).sum()
            if turn > 1e-9:
                e *= 1 - cost * turn
                to[d] = turn
                h = tw.copy()
        r = intr[d]
        p = h @ r
        if h.any():
            e *= 1 + p
            h = h * (1 + r) / (1 + p)
            if dd:
                e *= 1 - dd * h.sum()
        eq[d], ex[d] = e, h.sum()
    idx = target.index
    return pd.Series(eq, idx), pd.Series(ex, idx), pd.Series(to, idx)


def simulate_dca(panel, coin, cost, drag, start=None, end=None):
    """$1 every week (Sunday decision, Monday-open buy). Returns value, invested, cashflows."""
    O, C = panel["open"][coin], panel["close"][coin]
    idx = C.loc[start:end].dropna().index
    units, invested, flows = 0.0, 0.0, []
    val, inv = [], []
    first = True
    for d in idx:
        if drag:
            units *= 1 - drag / ANN
        if d.dayofweek == 0 and not first and not np.isnan(O[d]):
            units += (1 - cost) / O[d]
            invested += 1
            flows.append((d, -1.0))
        first = False
        val.append(units * C[d])
        inv.append(invested)
    val = pd.Series(val, idx)
    flows.append((idx[-1], float(val.iloc[-1])))
    return val, pd.Series(inv, idx), flows


def xirr(flows) -> float:
    t0 = flows[0][0]
    ts = np.array([(d - t0).days / 365.25 for d, _ in flows])
    cf = np.array([f for _, f in flows])
    f = lambda r: (cf / (1 + r) ** ts).sum()  # noqa: E731
    lo, hi = -0.99, 50.0
    if f(lo) * f(hi) > 0:
        return float("nan")
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(lo) * f(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# --------------------------------------------------------------------------- metrics

def cal_years(eq: pd.Series) -> pd.Series:
    ye = eq.groupby(eq.index.year).last()
    prev = ye.shift(1)
    prev.iloc[0] = eq.iloc[0]
    return ye / prev - 1


def metrics(eq, ex, to, a=None, b=None, rally_year=None):
    e = eq.loc[a:b]
    if len(e) < 30:
        return None
    x, t = ex.loc[a:b], to.loc[a:b]
    e = e / e.iloc[0]
    yrs = (e.index[-1] - e.index[0]).days / 365.25
    r = e.pct_change().dropna()
    cagr = e.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else float("nan")
    vol = r.std() * math.sqrt(ANN)
    sharpe = r.mean() / r.std() * math.sqrt(ANN) if r.std() > 0 else float("nan")
    mdd = (e / e.cummax() - 1).min()
    cy = cal_years(e)
    out = dict(cagr=cagr, vol=vol, sharpe=sharpe, mdd=mdd, worst_y=cy.min(), best_y=cy.max(),
               worst_y_n=int(cy.idxmin()), best_y_n=int(cy.idxmax()),
               invested=(x > 1e-6).mean(), expo=x.mean(),
               trades_py=(t > 1e-9).sum() / yrs, turn_py=t.sum() / yrs,
               y2022=cy.get(2022, float("nan")),
               rally=cy.get(rally_year, float("nan")) if rally_year else float("nan"),
               total=e.iloc[-1] - 1, years=yrs)
    return out


# --------------------------------------------------------------------------- formatting

def pct(v, d=1):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return f"{v * 100:+.{d}f}%"


def num(v, d=2):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return f"{v:.{d}f}"


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


# --------------------------------------------------------------------------- entry analysis

def entry_rows(panel, coin_w: pd.DataFrame, decision: pd.Series, ext_thr: float):
    """Every 0 -> >0 transition per coin at a decision day: extension vs SMA50 at the signal
    close, forward asset return from the next open over 30/90 days, and the realized trade
    return from entry open to exit open (open trades marked to the last close)."""
    O, C = panel["open"], panel["close"]
    rows = []
    for coin in coin_w.columns:
        w = coin_w[coin].where(decision).ffill().fillna(0.0).to_numpy()
        if not w.any():
            continue
        c, o = C[coin].to_numpy(), O[coin].to_numpy()
        ext = (C[coin] / sma(C[coin], 50) - 1).to_numpy()
        T = len(w)
        i = 1
        while i < T - 1:
            if w[i] > 0 and w[i - 1] == 0:
                ent = o[i + 1]
                j = i + 1
                while j < T and w[j] > 0:
                    j += 1
                exit_px = o[j + 1] if j + 1 < T else c[-1]
                f30 = c[i + 1 + 30] / ent - 1 if i + 31 < T else np.nan
                f90 = c[i + 1 + 90] / ent - 1 if i + 91 < T else np.nan
                if not np.isnan(ent) and not np.isnan(ext[i]):
                    rows.append(dict(coin=coin, date=C.index[i], ext=ext[i], f30=f30, f90=f90,
                                     trade=exit_px / ent - 1))
                i = j
            else:
                i += 1
    df = pd.DataFrame(rows)
    return df


def summarize_entries(df, ext_thr):
    out = []
    if df.empty:
        return [("all", 0, None), (f">={ext_thr:.0%}", 0, None)]
    for lab, sub in [("all entries", df), (f"ext>={ext_thr:.0%}", df[df.ext >= ext_thr])]:
        if sub.empty:
            out.append((lab, 0, None))
            continue
        s = {}
        for k in ["f30", "f90", "trade"]:
            v = sub[k].dropna()
            s[k] = (len(v), v.median() if len(v) else np.nan, v.mean() if len(v) else np.nan,
                    (v > 0).mean() if len(v) else np.nan, v.quantile(0.1) if len(v) else np.nan)
        out.append((lab, len(sub), s))
    return out


# --------------------------------------------------------------------------- stop-loss study
# ETF-execution approximation (IBIT/ETHA trade weekdays 09:30-16:00 ET; the coin trades 24/7):
# a weekday UTC coin bar stands in for the ETF session. Base-strategy orders fill at the next
# WEEKDAY open (a Fri/Sat/Sun signal fills Monday, acting on the Sunday close). A resting stop
# fills at the stop price when the weekday LOW breaches it, or at the OPEN if the open is
# already through it (gap). A breach on Sat/Sun fills Monday at min(Monday open, stop).
# US market holidays are ignored.

STOP_VARIANTS = (
    [("none", "none", None)]
    + [(f"fixed {p:.0%}", "fixed", p) for p in (0.08, 0.12, 0.15, 0.20)]
    + [(f"chandelier {k}xATR", "chand", k) for k in (2.5, 3.0, 4.0, 5.0)]
    + [(f"trail {p:.0%}", "trail", p) for p in (0.10, 0.15, 0.20, 0.25)]
    + [("hard 25%", "fixed", 0.25), ("chand 3xATR + 20% floor", "combo", 3.0)]
)
FAMILIES = {"fixed": [f"fixed {p:.0%}" for p in (0.08, 0.12, 0.15, 0.20)],
            "chandelier": [f"chandelier {k}xATR" for k in (2.5, 3.0, 4.0, 5.0)],
            "trail": [f"trail {p:.0%}" for p in (0.10, 0.15, 0.20, 0.25)]}


def atr(h, lo, c, n=14):
    tr = pd.concat([h - lo, (h - c.shift(1)).abs(), (lo - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def simulate_stops(panel, coin, base, kind, param, reentry, cost, drag, target_vol=0.50):
    """One-asset, long-or-cash ETF-style simulation of a base strategy with a stop layered on.

    base: 'sma' (100% when close > SMA50) or 'vt' (min(1, target_vol/30d vol), resized weekly).
    reentry after a stop-out: 'cross' = base signal must re-fire fresh (a close below SMA50,
    then a close back above); 'high' = a close above the stop-out day's high (with base on).
    Stops ratchet (never lowered). Returns equity/exposure/turnover series + trade list.
    """
    O, H, L, C = (panel[f][coin] for f in ("open", "high", "low", "close"))
    idx = C.index
    s50 = sma(C, 50)
    on = (C > s50).to_numpy()
    wt = (np.minimum(1.0, target_vol / ann_vol(C)).fillna(0.0).to_numpy()
          if base == "vt" else np.ones(len(C)))
    A = atr(H, L, C).to_numpy()
    o, h, lo, c = O.to_numpy(), H.to_numpy(), L.to_numpy(), C.to_numpy()
    dow = idx.dayofweek.to_numpy()
    T = len(C)
    eq, ex, to = np.ones(T), np.zeros(T), np.zeros(T)
    cash, units = 1.0, 0.0
    stop = np.nan
    peak = entry_px = entry_i = dist0 = None
    wk_breach = False
    lock = None          # None | ('cross', seen_below) | ('high', level)
    trades = []

    def sell(i, px, reason):
        nonlocal cash, units, stop, wk_breach, lock
        val = units * px
        to[i] += val / max(cash + val, 1e-12)
        cash += val * (1 - cost)
        trades.append(dict(entry_i=entry_i, exit_i=i, entry=entry_px, exit=px, reason=reason,
                           ret=px * (1 - cost) / (entry_px * (1 + cost)) - 1, dist0=dist0))
        units, stop, wk_breach = 0.0, np.nan, False
        if reason == "stop":
            lock = ["cross", False] if reentry == "cross" else ["high", h[i]]

    def stop_level(i_prev):
        if kind == "none":
            return np.nan
        if kind == "fixed":
            return entry_px * (1 - param)
        if kind == "trail":
            return peak * (1 - param)
        ch = peak - param * A[i_prev]
        return max(ch, entry_px * 0.80) if kind == "combo" else ch

    for i in range(1, T):
        if np.isnan(c[i]) or np.isnan(o[i]):
            eq[i] = eq[i - 1]
            continue
        weekday = dow[i] < 5
        if weekday:
            # 1) stop fills at the open: weekend breach, or an overnight gap through the stop
            if units > 0 and (wk_breach or o[i] <= stop):
                sell(i, min(o[i], stop), "stop")
            # 2) base-strategy orders from the latest close (Sunday's for a Monday)
            sig = on[i - 1] and not np.isnan(s50.iloc[i - 1])
            if lock is not None:
                if lock[0] == "cross":
                    if not on[i - 1]:
                        lock[1] = True
                    elif lock[1]:
                        lock = None
                elif c[i - 1] > lock[1]:
                    lock = None
            want = wt[i - 1] if (sig and lock is None) else 0.0
            eqv = cash + units * o[i]
            if units > 0 and want == 0.0:
                sell(i, o[i], "base")
            elif units == 0 and want > 0:
                units = want * eqv / (o[i] * (1 + cost))
                cash -= units * o[i] * (1 + cost)
                to[i] += want
                entry_px, entry_i, peak = o[i], i, o[i]
                stop = stop_level(i - 1)
                dist0 = (1 - stop / entry_px) if not np.isnan(stop) else np.nan
            elif units > 0 and base == "vt" and dow[i] == 0:
                cur = units * o[i] / eqv
                if abs(want - cur) > 1e-6:
                    d_units = (want - cur) * eqv / o[i]
                    cash -= d_units * o[i] + abs(d_units) * o[i] * cost
                    units += d_units
                    to[i] += abs(want - cur)
            # 3) intraday stop
            if units > 0 and lo[i] <= stop:
                sell(i, stop, "stop")
        elif units > 0 and lo[i] <= stop:
            wk_breach = True  # ETF closed: fills Monday at min(open, stop)
        if units > 0:
            if drag:
                units *= 1 - drag / ANN
            if not wk_breach:
                peak = max(peak, c[i])
                new = stop_level(i)
                stop = new if np.isnan(stop) else max(stop, new)
        eq[i] = cash + units * c[i]
        ex[i] = units * c[i] / eq[i] if eq[i] > 0 else 0.0
    if units > 0:
        trades.append(dict(entry_i=entry_i, exit_i=T - 1, entry=entry_px, exit=c[-1],
                           reason="open", ret=c[-1] / entry_px - 1, dist0=dist0))
    return (pd.Series(eq, idx), pd.Series(ex, idx), pd.Series(to, idx), trades)


def stop_stats(panel, coin, res, a=None, b=None):
    eq, ex, to, trades = res
    m = metrics(eq, ex, to, a, b)
    idx = eq.index
    a_ts = pd.Timestamp(a) if a else idx[0]
    b_ts = pd.Timestamp(b) if b else idx[-1]
    tr = [t for t in trades if a_ts <= idx[t["exit_i"]] <= b_ts]
    stops = [t for t in tr if t["reason"] == "stop"]
    c = panel["close"][coin].to_numpy()
    wd = idx.dayofweek.to_numpy() < 5
    whip = 0
    for t in stops:
        i = t["exit_i"]
        nxt = [c[j] for j in range(i + 1, len(c)) if wd[j]][:10]
        if nxt and max(nxt) > t["exit"]:
            whip += 1
    losing = [t["ret"] for t in stops if t["ret"] < 0]
    dists = [t["dist0"] for t in tr if not np.isnan(t["dist0"])]
    m.update(worst_trade=min((t["ret"] for t in tr), default=np.nan),
             avg_stop_ret=np.mean([t["ret"] for t in stops]) if stops else np.nan,
             avg_stop_loss=np.mean(losing) if losing else np.nan,
             n_losing_stops=len(losing),
             worst_stop=min((t["ret"] for t in stops), default=np.nan),
             stops_py=len(stops) / m["years"], n_stops=len(stops), n_trades=len(tr),
             whipsaw=whip / len(stops) if stops else np.nan,
             dist_med=np.median(dists) if dists else np.nan,
             dist_p90=np.percentile(dists, 90) if dists else np.nan)
    return m


def run_stop_study(args, panel):
    C = panel["close"]
    regs = {"ETF": (args.etf_cost, args.etf_drag), "RH": (args.rh_cost, 0.0)}
    cache = {}

    def res(coin, base, var, reentry, reg):
        k = (coin, base, var[0], reentry, reg)
        if k not in cache:
            cost, drag = regs[reg]
            cache[k] = simulate_stops(panel, coin, base, var[1], var[2], reentry, cost, drag)
        return cache[k]

    start = {coin: C[coin].first_valid_index() + pd.Timedelta(days=200) for coin in ("BTC", "ETH")}
    cells = [(coin, base) for coin in ("BTC", "ETH") for base in ("sma", "vt")]
    base_lab = {"sma": "SMA50 filter", "vt": "vol-targeted SMA50 (50% vol)"}

    # Re-entry rule chosen on <=2021 data only: median in-sample Sharpe over every stop variant,
    # asset and base, ETF costs.
    med = {}
    for rr in ("cross", "high"):
        shs = [stop_stats(panel, coin, res(coin, base, v, rr, "ETF"), start[coin], IS_END)["sharpe"]
               for coin, base in cells for v in STOP_VARIANTS if v[1] != "none"]
        med[rr] = float(np.nanmedian(shs))
    rr = max(med, key=med.get)
    other = "high" if rr == "cross" else "cross"

    L = []
    w = L.append
    w("## Stop-loss study\n")
    w(f"Generated {dt.date.today()} by `scripts/bakeoff_crypto.py --stops`. Base strategies: SMA50 "
      "filter and vol-targeted SMA50 filter on BTC and ETH, each with a stop layered ON TOP of "
      "the base exit. Primary costs: ETF proxy "
      f"{args.etf_cost:.2%}/side + {args.etf_drag:.2%}/yr; secondary: Robinhood {args.rh_cost:.2%}/side. "
      f"Scored from listing + 200d (BTC {start['BTC'].date()}, ETH {start['ETH'].date()}); "
      f"out-of-sample = {OOS_START} onward.\n")
    w("**Execution approximation (stated plainly):** IBIT/ETHA trade weekdays 09:30–16:00 ET; the "
      "coin trades 24/7. Each weekday UTC coin bar stands in for the ETF session. Base-strategy "
      "orders fill at the next weekday open (Fri/Sat/Sun signals → Monday open, acting on the "
      "Sunday close). A resting stop fills at the stop price when the weekday LOW breaches it, or "
      "at the OPEN if the open is already below it (gap). A breach on Sat/Sun fills at Monday's "
      "open or the stop, whichever is LOWER (conservative: a weekend dip that fully recovers "
      "still stops you at the stop). UTC day ≠ ET session, US holidays ignored, ETF premium/"
      "discount and tracking ignored. Stops ratchet up only; trailing peaks use closes. Because "
      "base trades now also wait for a weekday, the 'none' row differs slightly from the "
      "main tables above.\n")
    w("**Data cleaning:** two exchange flash-crash prints (BTC 2017-04-15 low ≈ $0; GDAX ETH "
      "2017-06-21 low $0.10) are clipped to the candle body — left in, the ETH one 'fills' an "
      "8% stop set at $8.85 in January while ETH traded ~$350, a -97% equity hit no ETF could "
      "produce.\n")
    w(f"**Re-entry after a stop-out:** tested both — (a) *cross*: the base signal must re-fire "
      f"fresh (a close below SMA50, then a close back above); (b) *high*: a close above the "
      f"stop-out day's high while the base signal is on. Picked on ≤2021 data only (median "
      f"in-sample Sharpe across all stop variants, assets, bases, ETF costs): cross "
      f"{med['cross']:.2f} vs high {med['high']:.2f} → **{rr}** is used below. "
      "Full-period comparison of the two rules is in the last table.\n")

    hdr = ["Variant", "CAGR", "Sharpe", "MaxDD", "Worst trade", "Avg stopped-trade ret",
           "Avg loss (losing stop-outs)", "Worst stopped", "Stop-outs/yr", "Whipsaw ≤10d", "OOS CAGR", "OOS Sharpe", "OOS MaxDD"]
    chosen = {}
    for coin, base in cells:
        for reg in ("ETF", "RH"):
            rows = []
            stats_is = {}
            for v in STOP_VARIANTS:
                r = res(coin, base, v, rr, reg)
                m = stop_stats(panel, coin, r, start[coin])
                mo = stop_stats(panel, coin, r, OOS_START)
                stats_is[v[0]] = stop_stats(panel, coin, r, start[coin], IS_END)
                stats_is[v[0]]["full"], stats_is[v[0]]["oos"] = m, mo
                rows.append([v[0], pct(m["cagr"]), num(m["sharpe"]), pct(m["mdd"], 0),
                             pct(m["worst_trade"], 0), pct(m["avg_stop_ret"], 1),
                             f"{pct(m['avg_stop_loss'], 1)} (n={m['n_losing_stops']})",
                             pct(m["worst_stop"], 0), num(m["stops_py"], 1),
                             "n/a" if math.isnan(m["whipsaw"]) else f"{m['whipsaw']:.0%}",
                             pct(mo["cagr"]), num(mo["sharpe"]), pct(mo["mdd"], 0)])
            w(f"### {coin} — {base_lab[base]} — {'ETF proxy costs' if reg == 'ETF' else 'Robinhood costs'}\n")
            w(md_table(hdr, rows) + "\n")
            # walk-forward per family
            wf = []
            for fam, names in FAMILIES.items():
                pick = max(names, key=lambda n: stats_is[n]["sharpe"])
                s = stats_is[pick]
                wf.append([fam, pick, num(s["sharpe"]), pct(s["oos"]["cagr"]),
                           num(s["oos"]["sharpe"]), pct(s["oos"]["mdd"], 0),
                           f"{min(stats_is[n]['oos']['sharpe'] for n in names):.2f} … "
                           f"{max(stats_is[n]['oos']['sharpe'] for n in names):.2f}"])
            for n in ("none", "hard 25%", "chand 3xATR + 20% floor"):
                s = stats_is[n]
                wf.append(["(fixed rule)", n, num(s["sharpe"]), pct(s["oos"]["cagr"]),
                           num(s["oos"]["sharpe"]), pct(s["oos"]["mdd"], 0), "—"])
            best = max((v[0] for v in STOP_VARIANTS if v[1] != "none"),
                       key=lambda n: stats_is[n]["sharpe"])
            chosen[(coin, base, reg)] = (best, stats_is)
            w(f"Walk-forward (parameter picked on ≤{IS_END} Sharpe, scored {OOS_START}+). "
              f"Best stop variant of all on ≤2021: **{best}** "
              f"(OOS Sharpe {num(stats_is[best]['oos']['sharpe'])} vs none "
              f"{num(stats_is['none']['oos']['sharpe'])}).\n")
            w(md_table(["Family", "Chosen (≤2021)", "IS Sharpe", "OOS CAGR", "OOS Sharpe",
                        "OOS MaxDD", "OOS Sharpe range in family"], wf) + "\n")

    # ---- risk-per-trade sizing
    w("### Risk-per-trade sizing (ETF costs, variant chosen on ≤2021 per asset × base)\n")
    w("Position size = account × risk budget ÷ stop distance d (d = stop distance at entry; for "
      "ATR stops it varies per trade, the median is used). Loss per stop-out = position × "
      "realized loss on LOSING stop-outs (includes gap-through and weekend fills, so it can "
      "exceed d); trailing stops that exit in profit are excluded from the average. "
      f"Share counts use IBIT ≈ ${args.ibit_px:.0f}"
      + (f", ETHA ≈ ${args.etha_px:.2f}" if args.etha_px else " (ETHA price not supplied — pass "
         "--etha-px; ETH share counts omitted)")
      + ". Only whole-share positions can carry a resting stop at Robinhood; a fractional "
      "remainder needs a synthetic stop checked by our code.\n")
    rows = []
    for coin, base in cells:
        best, st = chosen[(coin, base, "ETF")]
        m = st[best]["full"]
        d = m["dist_med"]
        px = args.ibit_px if coin == "BTC" else args.etha_px
        for rb in (0.01, 0.02, 0.05):
            frac = min(1.0, rb / d) if d and not math.isnan(d) else float("nan")
            avg_l = frac * m["avg_stop_loss"] if not math.isnan(m["avg_stop_loss"]) else float("nan")
            worst_l = (frac * min(m["worst_stop"], 0.0) if not math.isnan(m["worst_stop"])
                       else float("nan"))
            cells_acct = []
            for acct in (100, 5000):
                pos = acct * frac
                if px:
                    sh = int(pos // px)
                    cells_acct.append(f"${pos:,.0f} → {sh} sh (${sh * px:,.0f}) + ${pos - sh * px:,.0f} frac")
                else:
                    cells_acct.append(f"${pos:,.0f}")
            rows.append([f"{coin} {base_lab[base]}", best, f"{d:.1%} (p90 {m['dist_p90']:.1%})",
                         f"{rb:.0%}", f"{frac:.0%}", pct(avg_l, 2), pct(worst_l, 2)] + cells_acct)
    w(md_table(["Sleeve", "Stop", "Median d (p90)", "Risk budget", "Position % equity",
                "Avg acct loss / stop-out", "Worst acct loss / stop-out", "$100 account",
                "$5,000 account"], rows) + "\n")

    # ---- re-entry comparison
    rows = []
    for coin, base in cells:
        for rule in (rr, other):
            shs = [stop_stats(panel, coin, res(coin, base, v, rule, "ETF"), start[coin])["sharpe"]
                   for v in STOP_VARIANTS if v[1] != "none"]
            oos = [stop_stats(panel, coin, res(coin, base, v, rule, "ETF"), OOS_START)["sharpe"]
                   for v in STOP_VARIANTS if v[1] != "none"]
            rows.append([f"{coin} {base_lab[base]}", rule, num(float(np.median(shs))),
                         num(float(np.median(oos)))])
    w("### Re-entry rule comparison (ETF costs, median over stop variants)\n")
    w(md_table(["Sleeve", "Re-entry", "Median full-period Sharpe", "Median OOS Sharpe"], rows)
      + "\n")

    out = Path(args.out)
    text = out.read_text() if out.exists() else ""
    cut = text.find("## Stop-loss study")
    if cut >= 0:
        text = text[:cut].rstrip() + "\n\n"
    out.write_text(text + "\n".join(L))
    print(f"appended stop-loss study to {out}", file=sys.stderr)
    return chosen, rr, med


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--no-fetch", action="store_true", help="use cached candles only")
    ap.add_argument("--rh-cost", type=float, default=0.0095, help="Robinhood per-side cost")
    ap.add_argument("--cb-fee", type=float, default=0.006, help="Coinbase Advanced taker fee")
    ap.add_argument("--cb-slip", type=float, default=0.0002, help="Coinbase slippage per side")
    ap.add_argument("--etf-cost", type=float, default=0.0003, help="ETF proxy per-side cost")
    ap.add_argument("--etf-drag", type=float, default=0.0025, help="ETF expense ratio / yr")
    ap.add_argument("--ext", type=float, default=0.15, help="'extended' threshold vs SMA50")
    ap.add_argument("--out", default=str(OUT_DIR / "crypto_results.md"))
    ap.add_argument("--stops", action="store_true",
                    help="run only the stop-loss study and (re)write its section of --out")
    ap.add_argument("--ibit-px", type=float, default=49.0, help="IBIT share price for sizing")
    ap.add_argument("--etha-px", type=float, default=None, help="ETHA share price for sizing")
    args = ap.parse_args()

    print("loading candles ...", file=sys.stderr)
    panel, info = build_panel(fetch=not args.no_fetch)
    C = panel["close"]
    last = C.index[-1]
    if args.stops:
        run_stop_study(args, panel)
        return

    regimes = {
        "RH": dict(cost=args.rh_cost, drag=0.0, label=f"Robinhood {args.rh_cost:.2%}/side"),
        "CB": dict(cost=args.cb_fee + args.cb_slip, drag=0.0,
                   label=f"Coinbase Adv {args.cb_fee:.2%}+{args.cb_slip:.2%} slip/side"),
        "ETF": dict(cost=args.etf_cost, drag=args.etf_drag,
                    label=f"ETF proxy {args.etf_cost:.2%}/side + {args.etf_drag:.2%}/yr"),
        "0": dict(cost=0.0, drag=0.0, label="gross"),
    }

    # rally year = each asset's best full calendar year of buy & hold
    def rally_year(coin):
        cy = cal_years(C[coin].dropna())
        first_full = C[coin].first_valid_index().year + 1
        cy = cy[(cy.index >= first_full) & (cy.index < last.year)]
        return int(cy.idxmax())
    ry = {"BTC": rally_year("BTC"), "ETH": rally_year("ETH")}

    # name -> (builder(params) -> (target, rebal), asset for rally/ETF, default params, grid)
    D50 = dict(n=50)
    strategies = {
        "BTC buy&hold": (lambda p: s_buyhold(panel, "BTC"), "BTC", {}, [{}]),
        "BTC SMA filter": (lambda p: s_sma_filter(panel, "BTC", **p), "BTC", D50,
                           [dict(n=n) for n in (20, 50, 100, 150, 200)]),
        "BTC SMA cross": (lambda p: s_cross(panel, "BTC", **p), "BTC", dict(fast=20, slow=100),
                          [dict(fast=f, slow=s) for f, s in ((10, 50), (20, 100), (50, 200))]),
        "BTC pullback": (lambda p: s_pullback(panel, "BTC", **p), "BTC",
                         dict(x=0.03, trend="px200", exit_rule="px200"),
                         [dict(x=x, trend=tr, exit_rule=er) for x in (0.0, 0.03, 0.05)
                          for tr in ("px200", "gc") for er in ("px200", "px50", "dc")]),
        "BTC voltarget SMA50": (lambda p: s_voltarget(panel, "BTC", **p), "BTC",
                                dict(target=0.50), [dict(target=t) for t in (0.3, 0.5, 0.7)]),
        "ETH buy&hold": (lambda p: s_buyhold(panel, "ETH"), "ETH", {}, [{}]),
        "ETH SMA filter": (lambda p: s_sma_filter(panel, "ETH", **p), "ETH", D50,
                           [dict(n=n) for n in (20, 50, 100, 150, 200)]),
        "ETH pullback": (lambda p: s_pullback(panel, "ETH", **p), "ETH",
                         dict(x=0.03, trend="px200", exit_rule="px200"),
                         [dict(x=x, trend=tr, exit_rule=er) for x in (0.0, 0.03, 0.05)
                          for tr in ("px200", "gc") for er in ("px200", "px50", "dc")]),
        "Top-coins Donchian": (lambda p: s_donchian(panel, **p)[:2], None,
                               dict(lookbacks=(20, 50, 100)),
                               [dict(lookbacks=lb) for lb in
                                ((10, 20, 50), (20, 50, 100), (50, 100, 200))]),
        "Top-coins Donchian (full-invest)": (
            lambda p: s_donchian(panel, full_invest=True, **p)[:2], None,
            dict(lookbacks=(20, 50, 100)),
            [dict(lookbacks=lb) for lb in ((10, 20, 50), (20, 50, 100), (50, 100, 200))]),
    }

    def key(p):
        return json.dumps(p, sort_keys=True, default=str)

    runs = {}  # (name, paramkey, regime) -> (eq, ex, to)
    targets = {}

    def run(name, p, reg):
        k = (name, key(p), reg)
        if k not in runs:
            fn, asset, _, _ = strategies[name]
            tk = (name, key(p))
            if tk not in targets:
                targets[tk] = fn(p)
            tgt, rb = targets[tk]
            rg = regimes[reg]
            runs[k] = simulate(panel, tgt, rb, rg["cost"], rg["drag"])
        return runs[k]

    # Evaluate every strategy from its asset's listing + 200d (SMA200 warm-up) so rules and
    # buy & hold are scored over the same window; the Donchian book starts on BTC's.
    WARM = 200
    eval_start = {n: C[a or "BTC"].first_valid_index() + pd.Timedelta(days=WARM)
                  for n, (_, a, _, _) in strategies.items()}

    def M(name, p, reg, a=None, b=None, rally_year=None):
        a = max(pd.Timestamp(a), eval_start[name]) if a else eval_start[name]
        return metrics(*run(name, p, reg), a=a, b=b, rally_year=rally_year)

    def applicable(name, reg):
        return not (reg == "ETF" and strategies[name][1] is None)

    print("simulating ...", file=sys.stderr)
    L = []
    w = L.append
    w(f"# Crypto strategy bake-off — generated {dt.date.today()} by `scripts/bakeoff_crypto.py`\n")
    w(f"Data: Coinbase Exchange daily candles (UTC days), last complete day **{last.date()}**. "
      "Signals on day-t close, trades at day t+1 open. Long-or-cash, no leverage, cash yields 0, "
      "**Sharpe uses rf = 0**. Costs per unit turnover per side.\n")
    w("Cost regimes: " + "; ".join(f"**{k}** = {v['label']}" for k, v in regimes.items()
                                   if k != "0") + ". ETF proxy applies to BTC/ETH only (spot ETFs "
      "only existed from 2024; pre-2024 it is a hypothetical friction level, and ETF weekend "
      "gaps are ignored).\n")
    w("> **Survivorship / hindsight:** the top-coins universe was chosen in 2026 from coins that "
      "survived and are on Robinhood. Every strategy and every parameter here is in-sample except "
      "the walk-forward block. Short history (~11y BTC, fewer for alts).\n")

    w("## Data coverage\n")
    rows = []
    for coin in UNIVERSE:
        i = info.get(coin)
        if i is None:
            rows.append([coin, "skipped (no Coinbase history)", "", "", "", ""])
        else:
            rows.append([coin, i["first"], i["last"], i["rows"], i["missing"], i["longest_gap"]])
    w(md_table(["Coin", "First Coinbase day", "Last", "Rows", "Missing days", "Longest gap (d)"],
               rows) + "\n")
    bc = C["BTC"]
    b50 = sma(bc, 50).iloc[-1]
    ext_now = bc.iloc[-1] / b50 - 1
    ec = C["ETH"]
    ext_eth = ec.iloc[-1] / sma(ec, 50).iloc[-1] - 1
    w(f"**Now ({last.date()}):** BTC close {bc.iloc[-1]:,.2f}, SMA50 {b50:,.2f} → "
      f"**{ext_now:+.1%}** vs SMA50; above SMA200: {bc.iloc[-1] > sma(bc, 200).iloc[-1]}. "
      f"ETH {ext_eth:+.1%} vs SMA50. Strongest full rally year: BTC {ry['BTC']}, ETH {ry['ETH']}.\n")

    # ---- main tables
    hdr = ["Strategy", "CAGR", "Vol", "Sharpe", "MaxDD", "Worst yr", "Best yr", "% invested",
           "Trades/yr", "Cost drag/yr", "2022", "Rally yr"]
    summary = {}
    for reg in ("RH", "CB", "ETF"):
        w(f"## Full history — net of {regimes[reg]['label']}\n")
        rows = []
        for name, (fn, asset, dp, _) in strategies.items():
            if not applicable(name, reg):
                continue
            yr = ry.get(asset or "BTC")
            m = M(name, dp, reg, rally_year=yr)
            g = M(name, dp, "0", rally_year=yr)
            summary[(name, reg)] = m
            rows.append([name + (f" {dp}" if dp else ""), pct(m["cagr"]), pct(m["vol"], 0),
                         num(m["sharpe"]), pct(m["mdd"], 0),
                         f"{pct(m['worst_y'], 0)} ({m['worst_y_n']})",
                         f"{pct(m['best_y'], 0)} ({m['best_y_n']})",
                         f"{m['invested']:.0%}", num(m["trades_py"], 1),
                         pct(g["cagr"] - m["cagr"], 1), pct(m["y2022"], 0),
                         f"{pct(m['rally'], 0)} ({yr})"])
        w(md_table(hdr, rows) + "\n")
    w("Notes: every strategy is scored from its asset's first Coinbase day + 200d warm-up "
      f"(BTC {eval_start['BTC buy&hold'].date()}, ETH {eval_start['ETH buy&hold'].date()}; "
      "Donchian uses BTC's start with a universe that grows as coins list), so buy & hold and "
      "the rules share a window (so buy & hold's single entry cost lands in the warm-up and shows "
      "0 trades — worth ~0.1%/yr at RH cost); first/last calendar years are partial. "
      "Cost drag = gross CAGR − net CAGR. Trades/yr counts rebalance days with nonzero turnover "
      "(an entry and an exit are two). % invested = share of days with any exposure. "
      "Rally yr = the asset's best full calendar year of buy & hold (Donchian uses BTC's).\n")

    # ---- DCA
    w("## BTC weekly DCA ($1 every Monday open)\n")
    rows = []
    for (pl, a, b) in PERIODS:
        for reg in ("RH", "CB", "ETF"):
            rg = regimes[reg]
            val, inv, flows = simulate_dca(panel, "BTC", rg["cost"], rg["drag"],
                                           a or eval_start["BTC buy&hold"], b)
            ratio = (val / inv.replace(0, np.nan)).dropna()
            ratio_dd = (ratio / ratio.cummax() - 1).min() if len(ratio) else float("nan")
            bh = M("BTC buy&hold", {}, reg, a=a, b=b)
            rows.append([pl, reg, int(inv.iloc[-1]), f"{val.iloc[-1]:,.1f}",
                         f"{val.iloc[-1] / inv.iloc[-1]:.2f}x", pct(xirr(flows)),
                         pct(ratio_dd, 0), pct(bh["cagr"]) if bh else "n/a"])
    w(md_table(["Period", "Costs", "$ invested", "Final value", "Value/invested",
                "Money-weighted IRR", "Worst drawdown of value/invested", "B&H CAGR (TWR)"],
               rows) + "\n")
    w("DCA's time-weighted return is just B&H minus the entry cost on each lot, so the "
      "money-weighted IRR is the number that differs.\n")

    # ---- subperiods
    for reg in ("RH", "ETF"):
        w(f"## Subperiods — net of {regimes[reg]['label']} (CAGR / MaxDD / Sharpe)\n")
        rows = []
        for name, (fn, asset, dp, _) in strategies.items():
            if not applicable(name, reg):
                continue
            cells = [name]
            for pl, a, b in PERIODS[1:]:
                m = M(name, dp, reg, a, b)
                cells.append("n/a" if m is None else
                             f"{pct(m['cagr'], 0)} / {pct(m['mdd'], 0)} / {num(m['sharpe'], 1)}")
            rows.append(cells)
        w(md_table(["Strategy"] + [p[0] for p in PERIODS[1:]], rows) + "\n")
    w("Subperiod rows are slices of the continuously-running strategy (state carries in), "
      "so a position held on 1 Jan counts. 2025-26/09 is ~1.7 years.\n")

    # ---- pullback grid
    for coin in ("BTC", "ETH"):
        name = f"{coin} pullback"
        w(f"## {name} grid — full history, net of RH and ETF costs\n")
        rows = []
        for p in strategies[name][3]:
            mr = M(name, p, "RH")
            me = M(name, p, "ETF")
            rows.append([f"{p['x']:.0%}", p["trend"], p["exit_rule"], pct(mr["cagr"]),
                         num(mr["sharpe"]), pct(mr["mdd"], 0), f"{mr['invested']:.0%}",
                         num(mr["trades_py"], 1), pct(me["cagr"]), num(me["sharpe"])])
        w(md_table(["X", "Trend", "Exit", "CAGR (RH)", "Sharpe (RH)", "MaxDD (RH)", "% inv",
                    "Trades/yr", "CAGR (ETF)", "Sharpe (ETF)"], rows) + "\n")
    w("Trend: px200 = close > SMA200, gc = SMA50 > SMA200. Entry: close ≤ SMA50·(1+X) or "
      "RSI14 < 40. Exit: px200 = close < SMA200, px50 = close < SMA50, dc = SMA50 < SMA200.\n")

    # ---- walk-forward
    w(f"## Walk-forward — params chosen on data ≤ {IS_END} (max in-sample Sharpe, same cost "
      f"regime), scored {OOS_START} → {last.date()}\n")
    oos_rows = {}
    for reg in ("RH", "ETF"):
        rows = []
        for name, (fn, asset, dp, grid) in strategies.items():
            if not applicable(name, reg):
                continue
            scored = []
            for p in grid:
                mi = M(name, p, reg, None, IS_END)
                mo = M(name, p, reg, OOS_START, None)
                scored.append((p, mi, mo))
            best = max(scored, key=lambda s: s[1]["sharpe"])
            dflt = next(s for s in scored if key(s[0]) == key(dp))
            oos_sh = sorted(s[2]["sharpe"] for s in scored)
            bh = M(f"{asset or 'BTC'} buy&hold", {}, reg, a=OOS_START)
            p, mi, mo = best
            oos_rows[(name, reg)] = mo
            rows.append([name, json.dumps(p) if p else "—", num(mi["sharpe"]), pct(mi["cagr"]),
                         pct(mo["cagr"]), num(mo["sharpe"]), pct(mo["mdd"], 0),
                         f"{pct(dflt[2]['cagr'])} / {num(dflt[2]['sharpe'])}",
                         f"{num(oos_sh[0])} … {num(oos_sh[-1])}",
                         f"{pct(bh['cagr'])} / {num(bh['sharpe'])}"])
        w(f"### {regimes[reg]['label']}\n")
        w(md_table(["Strategy", "Chosen (≤2021)", "IS Sharpe", "IS CAGR", "OOS CAGR",
                    "OOS Sharpe", "OOS MaxDD", "OOS default-param CAGR/Sharpe",
                    "OOS Sharpe range over grid", "OOS B&H CAGR/Sharpe (same asset)"],
                   rows) + "\n")

    # ---- extended entries
    w(f"## Entering after a big rally — entries with close ≥ {args.ext:.0%} above SMA50\n")
    w("Forward returns are the ASSET's return from the entry open (not the strategy's), 30 and 90 "
      "calendar days out; 'trade' is the strategy's realized entry-open→exit-open return (open "
      "trades marked to last close). Gross of costs. Cells: n · median · mean · %>0 · p10.\n")

    def cell(s, k):
        if s is None or s[k][0] == 0:
            return "n=0"
        n, med, mean, pos, p10 = s[k]
        return f"{n} · {pct(med, 0)} · {pct(mean, 0)} · {pos:.0%} · {pct(p10, 0)}"

    rows = []
    wait_tables = {}
    for coin in ("BTC", "ETH"):
        c = C[coin]
        ext = c / sma(c, 50) - 1
        o = panel["open"][coin]
        f30 = c.shift(-31) / o.shift(-1) - 1
        f90 = c.shift(-91) / o.shift(-1) - 1
        hot = (ext >= args.ext).to_numpy()
        ep = np.zeros(len(hot), bool)
        last_ep = -10_000
        for i in range(1, len(hot)):
            # first day of a new >=thr run, at most one episode per 30 days (de-overlapped-ish)
            if hot[i] and not hot[i - 1] and i - last_ep >= 30:
                ep[i], last_ep = True, i
        ep = pd.Series(ep, index=c.index)
        since20 = c.index >= "2020-01-01"
        for lab, mask in [("every day", ext.notna()),
                          (f"every day ext≥{args.ext:.0%}", ext >= args.ext),
                          (f"every day ext≥{args.ext:.0%}, 2020+", (ext >= args.ext) & since20),
                          (f"episode starts (first day ≥{args.ext:.0%}, ≥30d apart)", ep)]:
            sub = pd.DataFrame({"f30": f30[mask], "f90": f90[mask]})
            s = {}
            for k in ("f30", "f90"):
                v = sub[k].dropna()
                s[k] = (len(v), v.median(), v.mean(), (v > 0).mean(), v.quantile(0.1))
            s["trade"] = (0, 0, 0, 0, 0)
            rows.append([f"{coin} (unconditional)", lab, int(mask.sum()), cell(s, "f30"),
                         cell(s, "f90"), "—"])
        # "wait for the pullback" vs "buy the extended day": from each episode start, does
        # price come back to <= SMA50*(1+3%) within 90d, and at what price vs the episode open?
        wait = []
        s50 = sma(c, 50)
        for d in ep[ep].index:
            i = c.index.get_loc(d)
            if i + 1 >= len(c):
                continue
            buy_now = o.iloc[i + 1]
            win = c.iloc[i + 1:i + 91]
            hit = win[win <= s50.iloc[i + 1:i + 91] * 1.03]
            full_window = i + 91 <= len(c)
            later = c.iloc[i + 90] if full_window else np.nan
            wait.append(dict(date=d.date(), ext=ext.iloc[i], buy_now=buy_now,
                             pull=hit.iloc[0] / buy_now - 1 if len(hit) else np.nan,
                             pull_date=hit.index[0].date() if len(hit) else None,
                             f90=later / buy_now - 1, full=full_window))
        wait_tables[coin] = wait
    entry_detail = {}
    for name, (fn, asset, dp, _) in strategies.items():
        if name.endswith("buy&hold"):
            continue
        tgt, rb = targets[(name, key(dp))]
        df = entry_rows(panel, tgt, rb, args.ext)
        entry_detail[name] = df
        for lab, n, s in summarize_entries(df, args.ext):
            rows.append([name, lab, n, cell(s, "f30"), cell(s, "f90"), cell(s, "trade")])
    w(md_table(["Strategy", "Group", "n", "fwd 30d", "fwd 90d", "Strategy trade"], rows) + "\n")
    w("Unconditional rows overlap heavily (consecutive days share most of their forward window), "
      "so their n overstates independent evidence.\n")

    for coin, wt in wait_tables.items():
        w(f"### {coin}: buy the first ≥{args.ext:.0%}-extended day, or wait for a pullback to "
          "≤ SMA50+3%?\n")
        rows = [[r["date"], pct(r["ext"], 0), f"{r['buy_now']:,.2f}",
                 r["pull_date"] or "none in 90d", pct(r["pull"], 0), pct(r["f90"], 0)]
                for r in wt]
        w(md_table(["Episode start", "Ext", "Next open (buy now)", "First pullback close",
                    "Pullback px vs buy-now", "fwd 90d from buy-now"], rows) + "\n")
        done = [r for r in wt if r["full"]]
        hits = [r for r in done if r["pull_date"]]
        cheaper = [r for r in hits if r["pull"] < 0]
        if done:
            w(f"{coin}: {len(done)} complete episodes; a pullback to ≤SMA50+3% came within 90d "
              f"in {len(hits)} ({len(hits) / len(done):.0%}); it was below the buy-now price in "
              f"{len(cheaper)}; no pullback in {len(done) - len(hits)} (median 90d return of "
              f"those: {pct(np.nanmedian([r['f90'] for r in done if not r['pull_date']]) if len(done) > len(hits) else float('nan'), 0)}).\n")

    ext_list = []
    for name, df in entry_detail.items():
        if df.empty:
            continue
        for _, r in df[df.ext >= args.ext].iterrows():
            ext_list.append([name, r["coin"], r["date"].date(), pct(r["ext"], 0), pct(r["f30"], 0),
                             pct(r["f90"], 0), pct(r["trade"], 0)])
    if ext_list:
        w(f"### Individual extended entries (BTC/ETH single-asset strategies)\n")
        single = [r for r in ext_list if not r[0].startswith("Top-coins")]
        w(md_table(["Strategy", "Coin", "Signal date", "Ext", "fwd30", "fwd90", "Trade"],
                   single) + "\n")

    w("## Caveats\n")
    w("- In-sample: the rules and default parameters are well-known ones evaluated on the same "
      "history that made them famous; only the walk-forward block is out-of-sample, and 2022+ "
      "is one bear and one-and-a-half bull markets.\n"
      "- Survivorship: the top-coins list is 2026 survivors on Robinhood. Dead/delisted coins "
      "(e.g. LUNA, FTT) never enter the universe, which flatters the Donchian ensemble.\n"
      "- Coinbase listing ≠ coin launch; early Coinbase BTC (2015) prints are thin. XRP's "
      "2021-2023 Coinbase halt shows as a gap (the coin is ineligible there).\n"
      "- Costs are flat per-side assumptions; Robinhood's real spread varies with volatility "
      "and size. ETF proxy pre-2024 is hypothetical. No taxes (every exit in a taxable account "
      "realizes gains; B&H defers them).\n"
      "- Daily bars, trade at next open: no intraday stops, no gap risk modelling beyond daily.\n")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L))
    print(f"wrote {out}", file=sys.stderr)

    # terse console summary
    print(f"\nlast day {last.date()}  BTC ext vs SMA50 {ext_now:+.1%}")
    for reg in ("RH", "ETF"):
        print(f"\n[{regimes[reg]['label']}]  CAGR  Sharpe  MaxDD")
        ranked = sorted(((n, m) for (n, r), m in summary.items() if r == reg),
                        key=lambda t: -t[1]["sharpe"])
        for n, m in ranked:
            print(f"  {n:36s} {pct(m['cagr']):>8s} {num(m['sharpe']):>6s} {pct(m['mdd'], 0):>6s}")


if __name__ == "__main__":
    main()
