#!/usr/bin/env python3
"""Options-income bake-off: the UPSIDE and the DOWNSIDE of each premium-selling approach, side by side.

Why this exists: the 2026-09 research (research/options-crypto-strategy-research-2026-09.md) ranked a
cash-secured-put / wheel program first, but almost all of its evidence is SPX index studies ending in
2014-2018 and quoted mostly as crash numbers. Before the owner (options beginner, Robinhood CASH account,
Level 2: CSPs, covered calls, long options, collars — NO spreads) commits capital, we want measured,
reproducible numbers on three layers of evidence, each weaker-but-closer-to-reality than the last:

  PART 1  Cboe benchmark strategy indexes (PUT, WPUT, BXM, BXMD, CLL, PPUT, CNDR, ...) vs an S&P 500
          total-return proxy. Real published index levels — the primary evidence. Full history + since
          2007: CAGR / vol / Sharpe / max DD + duration / calendar years / up- & down-capture.
  PART 2  Option-income ETFs the owner can actually buy for $100 via fractional shares (JEPI, JEPQ,
          QYLD, XYLD, SPYI, crypto covered-call ETFs). TOTAL return including distributions — these pay
          10-60%/yr distributions, so price-only charts are badly misleading — vs the underlying's TR.
  PART 3  Single-stock wheel replay on REAL historical option prices from the Robinhood MCP
          (get_option_instruments state=expired + get_option_historicals daily bars). No Black-Scholes-
          on-realized-vol price invention: BS is used ONLY to invert an observed option price into an
          implied vol / delta for strike selection.

Data sources (all keyless):
  Cboe CDN   https://cdn.cboe.com/api/global/us_indices/daily_prices/<SYM>_History.csv  (index levels)
             https://cdn.cboe.com/api/global/us_indices/definitions/all_indices.json    (Cboe's own
             one-line methodology descriptions)
  Yahoo      query1.finance.yahoo.com/v8/finance/chart (raw close + dividend events; we compute TR
             ourselves and cross-check against Robinhood closes)
  FRED       DTB3 (3-month T-bill) for Sharpe and for the BS inversion rate
  SEC EDGAR  data.sec.gov submissions: 8-K item 2.02 filing dates = historical earnings dates (RH's
             get_earnings_results only returns the trailing 8 quarters)
  Robinhood  scripts/rh_direct.py, READ-ONLY tools only (enforced by READ_ONLY_TOOLS below). This script
             never reviews or places an order.

Everything fetched is cached under data/bakeoff/ (gitignored); re-runs are offline unless --refresh.

Usage:
    python3 scripts/bakeoff_options.py                 # all three parts, writes data/bakeoff/options_results.md
    python3 scripts/bakeoff_options.py --parts 1,2     # skip the (slow, RH-dependent) wheel replay
    python3 scripts/bakeoff_options.py --haircut 0.10  # replay fills at mark -/+ 10% instead of 5%
    python3 scripts/bakeoff_options.py --refresh       # re-download Cboe/Yahoo/FRED (RH cache is kept)
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import io
import json
import math
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "data" / "bakeoff"
RAW = OUT_DIR / "raw"
RH_CACHE = OUT_DIR / "rh_cache"
REPORT = OUT_DIR / "options_results.md"
UA = "Mozilla/5.0 (agentic-trading bakeoff research script)"
SEC_UA = "agentic-trading-research bakeoff script admin@localhost"

CBOE_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{sym}_History.csv"
CBOE_DEFS = "https://cdn.cboe.com/api/global/us_indices/definitions/all_indices.json"
YAHOO_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
             "?period1=0&period2=9999999999&interval=1d&events=div%2Csplit")
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3"
EDGAR_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# (symbol, can the owner replicate the *structure* at options Level 2 in a cash account?)
# SPX itself is never replicable at retail size (1 SPX contract ~ $776k notional); this flag is about
# the option structure, which the owner could run on SPY/single names.
CBOE_INDEXES = [
    ("PUT", "yes - cash-secured put"), ("WPUT", "yes - weekly CSP"), ("PUTY", "yes - OTM CSP"),
    ("BXM", "yes - covered call"), ("BXMD", "yes - covered call"), ("BXY", "yes - covered call"),
    ("BXMW", "yes - covered call"), ("CLL", "yes - collar"),
    ("CLLZ", "NO - long put SPREAD (L3)"), ("PPUT", "yes - protective put"),
    ("CNDR", "NO - iron condor (L3)"), ("BFLY", "NO - iron butterfly (L3)"),
    ("BXN", "yes - covered call (NDX)"),
]
YEARS = [2008, 2013, 2017, 2020, 2021, 2022, 2023, 2024, 2025, 2026]
BULL_YEARS = [2013, 2017, 2021, 2023, 2024]
BEAR_YEARS = [2008, 2022]
COVID = ("2020-02-19", "2020-03-23")  # SPX peak -> trough
BENCH_START = "1993-01-29"            # first SPY day: SPX-TR proxy starts here

# ETF -> underlying used for the same-window comparison
ETFS = [
    ("JEPI", "SPY"), ("XYLD", "SPY"), ("SPYI", "SPY"), ("PUTW", "SPY"),
    ("JEPQ", "QQQ"), ("QYLD", "QQQ"), ("QQQI", "QQQ"),
    ("YBTC", "IBIT"), ("YBIT", "IBIT"), ("BTCI", "IBIT"), ("BTCC", "IBIT"), ("BAGY", "IBIT"),
    ("YETH", "ETHA"),
]

READ_ONLY_TOOLS = {
    "get_option_chains", "get_option_instruments", "get_option_historicals", "get_equity_historicals",
    "get_earnings_results", "search",
}


# --------------------------------------------------------------------------------------------- fetch
def http_get(url: str, ua: str = UA, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise
        except (TimeoutError, urllib.error.URLError, ConnectionError, http.client.HTTPException):
            # network hiccup: one curl attempt (different TLS stack) before backing off
            out = subprocess.run(["curl", "-sf", "-m", str(timeout), "-A", ua, url], capture_output=True)
            if out.returncode == 0 and out.stdout:
                return out.stdout
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"unreachable: {url}")


def cached(path: Path, url: str, refresh: bool, ua: str = UA) -> bytes | None:
    if path.exists() and not refresh:
        return path.read_bytes()
    try:
        body = http_get(url, ua)
    except urllib.error.HTTPError as e:
        print(f"  [miss] {url} -> HTTP {e.code}", file=sys.stderr)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    time.sleep(0.4)
    return body


def load_cboe(sym: str, refresh: bool) -> pd.Series | None:
    body = cached(RAW / f"{sym}_History.csv", CBOE_URL.format(sym=sym), refresh)
    if not body or not body.startswith(b"DATE"):
        return None
    df = pd.read_csv(io.BytesIO(body))
    s = pd.Series(df.iloc[:, 1].astype(float).values,
                  index=pd.to_datetime(df.iloc[:, 0], format="%m/%d/%Y"), name=sym).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    # Some files (PUT) carry a handful of sparse, irregular pre-history points before daily data
    # starts; returns across multi-year gaps would poison vol/Sharpe, so start at the first run of
    # genuinely daily prints.
    gaps = s.index.to_series().diff().dt.days
    for i in range(len(s) - 6):
        if gaps.iloc[i + 1:i + 6].max() <= 5:
            return s.iloc[i:]
    return s


def load_cboe_defs(refresh: bool) -> dict[str, dict]:
    body = cached(RAW / "all_indices.json", CBOE_DEFS, refresh)
    return {d.get("index_symbol"): d for d in json.loads(body)} if body else {}


def load_yahoo(sym: str, refresh: bool) -> pd.DataFrame | None:
    """Raw (split-adjusted, NOT dividend-adjusted) close + dividend cash on ex-date."""
    body = cached(RAW / f"yahoo_{sym}.json", YAHOO_URL.format(sym=sym), refresh)
    if not body:
        return None
    res = (json.loads(body).get("chart") or {}).get("result")
    if not res:
        return None
    r = res[0]
    idx = pd.to_datetime(r["timestamp"], unit="s").normalize()
    df = pd.DataFrame({"close": r["indicators"]["quote"][0]["close"],
                       "adjclose": r["indicators"]["adjclose"][0]["adjclose"]}, index=idx)
    df = df[~df.index.duplicated(keep="last")].dropna(subset=["close"])
    df["div"] = 0.0
    for ev in (r.get("events") or {}).get("dividends", {}).values():
        d = pd.to_datetime(ev["date"], unit="s").normalize()
        if d in df.index:
            df.loc[d, "div"] += float(ev["amount"])
    df.attrs["splits"] = list((r.get("events") or {}).get("splits", {}).values())
    df.attrs["name"] = r["meta"].get("longName")
    return df


def load_rf(refresh: bool) -> pd.Series:
    # FRED drops connections for browser-like custom UAs; a plain curl UA is accepted
    body = cached(RAW / "DTB3.csv", FRED_URL, refresh, ua="curl/8.7.1")
    df = pd.read_csv(io.BytesIO(body), na_values=".")
    s = pd.Series(df["DTB3"].values, index=pd.to_datetime(df["observation_date"])).astype(float)
    return (s / 100.0).ffill()


def total_return_index(df: pd.DataFrame) -> pd.Series:
    """TR_t = TR_{t-1} * (close_t + div_t) / close_{t-1}: distributions reinvested at the ex-date close."""
    r = (df["close"] + df["div"]) / df["close"].shift(1)
    return r.fillna(1.0).cumprod()


def spx_tr_proxy(refresh: bool) -> pd.Series:
    """S&P 500 total-return proxy: Cboe SPX price + SPY cash dividends reinvested on SPY ex-dates,
    as a yield (div / prior SPY close). Cboe does not publish SPXTR on its CDN (403), and the owner
    rule is keyless data only."""
    spx = load_cboe("SPX", refresh)
    spy = load_yahoo("SPY", refresh)
    yld = (spy["div"] / spy["close"].shift(1)).reindex(spx.index).fillna(0.0)
    r = spx.pct_change().fillna(0.0) + yld
    s = (1 + r[spx.index >= BENCH_START]).cumprod()
    s.name = "SPX-TR*"
    return s


# ------------------------------------------------------------------------------------------- metrics
def month_end(s: pd.Series) -> pd.Series:
    return s.resample("ME").last().dropna()


def capture(strat: pd.Series, bench: pd.Series) -> tuple[float, float]:
    """Morningstar-style up/down capture on monthly returns: annualized compound return of the
    strategy in benchmark-up (-down) months divided by the benchmark's in the same months."""
    a = pd.concat([month_end(strat), month_end(bench)], axis=1, join="inner").pct_change().dropna()
    a.columns = ["s", "b"]
    out = []
    for mask in (a["b"] > 0, a["b"] < 0):
        sub = a[mask]
        if len(sub) == 0:
            out.append(float("nan"))
            continue
        gs = (1 + sub["s"]).prod() ** (12 / len(sub)) - 1
        gb = (1 + sub["b"]).prod() ** (12 / len(sub)) - 1
        out.append(gs / gb if gb else float("nan"))
    return out[0], out[1]


def drawdown_stats(s: pd.Series) -> dict:
    peak = s.cummax()
    dd = s / peak - 1
    mdd = dd.min()
    trough = dd.idxmin()
    pk = s.loc[:trough].idxmax()
    rec = s.loc[trough:][s.loc[trough:] >= s.loc[pk]]
    recovered = len(rec) > 0
    end = rec.index[0] if recovered else s.index[-1]
    # longest underwater stretch of any drawdown
    under = dd < 0
    longest, cur_start = 0, None
    for d, u in under.items():
        if u and cur_start is None:
            cur_start = d
        elif not u and cur_start is not None:
            longest = max(longest, (d - cur_start).days)
            cur_start = None
    if cur_start is not None:
        longest = max(longest, (s.index[-1] - cur_start).days)
    return {"mdd": mdd, "mdd_peak": pk, "mdd_trough": trough, "mdd_recovered": recovered,
            "mdd_days": (end - pk).days, "longest_uw_days": longest}


def year_returns(s: pd.Series) -> dict[int, float]:
    ye = s.resample("YE").last()
    out = {}
    first_year = s.index[0].year
    for y in ye.index.year:
        if y == first_year and s.index[0] > pd.Timestamp(f"{y}-01-05"):
            continue  # partial first year
        prev = s[s.index < pd.Timestamp(f"{y}-01-01")]
        if len(prev) == 0:
            continue
        out[y] = ye[ye.index.year == y].iloc[0] / prev.iloc[-1] - 1
    return out


def window_ret(s: pd.Series, a: str, b: str) -> float:
    x = s[(s.index >= a) & (s.index <= b)]
    if len(x) < 2 or x.index[0] > pd.Timestamp(a) + pd.Timedelta(days=5):
        return float("nan")
    return x.iloc[-1] / x.iloc[0] - 1   # close-to-close from the SPX peak day to the trough day


def metrics(s: pd.Series, bench: pd.Series | None, rf: pd.Series) -> dict:
    s = s.dropna()
    r = s.pct_change().dropna()
    days = (s.index[-1] - s.index[0]).days
    cagr = (s.iloc[-1] / s.iloc[0]) ** (365.25 / days) - 1 if days > 0 else float("nan")
    vol = r.std() * math.sqrt(252)
    rfd = rf.reindex(r.index, method="ffill").fillna(0.0) / 252
    sharpe = (r - rfd).mean() * 252 / vol if vol else float("nan")
    m = {"start": s.index[0].date(), "end": s.index[-1].date(), "cagr": cagr, "vol": vol,
         "sharpe": sharpe, "total": s.iloc[-1] / s.iloc[0] - 1}
    m.update(drawdown_stats(s))
    yr = year_returns(s)
    full = {y: v for y, v in yr.items() if y < s.index[-1].year or s.index[-1].month == 12}
    m["years"] = yr
    if full:
        m["worst_year"] = min(full.items(), key=lambda kv: kv[1])
        m["best_year"] = max(full.items(), key=lambda kv: kv[1])
    mr = month_end(s).pct_change().dropna()
    m["worst_month"] = mr.min() if len(mr) else float("nan")
    m["covid"] = window_ret(s, *COVID)
    if bench is not None:
        m["up_cap"], m["down_cap"] = capture(s, bench)
    return m


# --------------------------------------------------------------------------------------- formatting
def pct(x, d=1) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x * 100:+.{d}f}%"


def num(x, d=2) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:.{d}f}"


def table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


# --------------------------------------------------------------------------------------------- PART 1
def part1(refresh: bool, rf: pd.Series) -> tuple[str, dict]:
    defs = load_cboe_defs(refresh)
    bench = spx_tr_proxy(refresh)
    spy = load_yahoo("SPY", refresh)
    spy_tr = total_return_index(spy)
    series = {"SPX-TR*": bench}
    missing = []
    for sym, _ in CBOE_INDEXES:
        s = load_cboe(sym, refresh)
        if s is None:
            missing.append(sym)
        else:
            series[sym] = s
    feas = dict(CBOE_INDEXES)
    feas["SPX-TR*"] = "yes - buy & hold (SPY/VOO)"

    L = ["## PART 1 — Cboe benchmark strategy indexes vs S&P 500 total return", ""]
    L.append("**Benchmark.** Cboe's CDN refuses SPXTR/SPTR (HTTP 403), so `SPX-TR*` = Cboe SPX price "
             "index + SPY cash dividends (Yahoo chart API) reinvested as a yield on SPY ex-dates, from "
             f"{BENCH_START} (SPY's first day). Check: SPX-TR* CAGR vs SPY's own total return (which "
             "pays the 0.09% expense ratio) over the same window: "
             f"{pct(metrics(bench, None, rf)['cagr'], 2)} vs "
             f"{pct(metrics(spy_tr[spy_tr.index >= BENCH_START], None, rf)['cagr'], 2)}.")
    L.append("")
    if missing:
        L.append(f"Not available on the Cboe CDN: {', '.join(missing)}.")
    L.append("")
    L.append("### What each index does (Cboe's own description, verbatim from all_indices.json)")
    L.append("")
    rows = []
    for sym, f in CBOE_INDEXES:
        d = defs.get(sym, {})
        desc = " ".join((d.get("description") or "").split())
        rows.append([sym, d.get("name", ""), desc or "(no description published)", f,
                     series[sym].index[0].date() if sym in series else "missing"])
    L.append(table(["Index", "Name", "Cboe description", "Owner can run the structure at L2?", "Daily data from"], rows))
    L.append("")
    L.append("Notes: all SPX indexes use European, cash-settled SPX options (no early assignment, no "
             "share delivery). Put-write/condor/butterfly indexes are collateralised in T-bills, so their "
             "return = option P&L + T-bill yield. The *structure* column says whether a Level-2 cash "
             "account could run the same structure on SPY/stocks — CLLZ/CNDR/BFLY need spreads (Level 3).")
    L.append("")

    results = {}
    for win_name, start in (("full", None), ("since2007", "2007-01-03")):
        results[win_name] = {}
        for sym, s in series.items():
            x = s[s.index >= max(pd.Timestamp(BENCH_START), s.index[0])]
            if start:
                x = x[x.index >= start]
            b = bench.reindex(x.index).dropna()
            x = x.reindex(b.index)
            if len(x) < 250:
                continue
            results[win_name][sym] = metrics(x, None if sym == "SPX-TR*" else b, rf)
            if sym == "SPX-TR*":
                results[win_name][sym]["up_cap"] = results[win_name][sym]["down_cap"] = 1.0

    order = ["SPX-TR*"] + [s for s, _ in CBOE_INDEXES if s in series]
    # Headline: upside vs downside, since 2007 (every index has daily data by then except CLL (Aug-2008),
    # BXMW (2012), BXN (2009) — those start later and are flagged).
    L.append("### Upside vs downside (since 2007-01-03, or index start if later)")
    L.append("")
    L.append("Upside = how much of the good times you keep; downside = how much of the bad times you eat. "
             "Up/down capture are monthly, Morningstar-style (1.00 = moves one-for-one with the S&P 500 TR).")
    L.append("")
    rows = []
    for sym in order:
        m = results["since2007"].get(sym)
        if not m:
            continue
        bull = [m["years"].get(y) for y in BULL_YEARS if m["years"].get(y) is not None]
        rows.append([sym + ("" if str(m["start"]) <= "2007-01-10" else f" (from {m['start']})"),
                     pct(m["cagr"]), num(m["up_cap"]), pct(np.mean(bull)) if bull else "n/a",
                     pct(m["years"].get(2013)), pct(m["years"].get(2021)), pct(m["years"].get(2023)),
                     num(m["down_cap"]), pct(m["years"].get(2008)), pct(m["covid"]),
                     pct(m["years"].get(2022)), pct(m["mdd"]), num(m["sharpe"])])
    L.append(table(["Index", "CAGR", "Up-capture", f"Avg bull yr {BULL_YEARS}", "2013", "2021", "2023",
                    "Down-capture", "2008", "COVID crash 2/19-3/23/20", "2022", "Max DD", "Sharpe"], rows))
    L.append("")

    for win_name, label in (("full", f"FULL history (from {BENCH_START} or index start if later)"),
                            ("since2007", "SINCE 2007-01-03")):
        L.append(f"### Risk/return — {label}")
        L.append("")
        rows = []
        for sym in order:
            m = results[win_name].get(sym)
            if not m:
                continue
            wy, by = m.get("worst_year", (None, float("nan"))), m.get("best_year", (None, float("nan")))
            rows.append([sym, f"{m['start']}", pct(m["cagr"]), pct(m["vol"]), num(m["sharpe"]),
                         pct(m["mdd"]), f"{m['mdd_peak'].date()}→{m['mdd_trough'].date()}",
                         f"{m['mdd_days']}d" + ("" if m["mdd_recovered"] else " (not recovered)"),
                         f"{m['longest_uw_days']}d", f"{wy[0]} {pct(wy[1])}", f"{by[0]} {pct(by[1])}",
                         num(m["up_cap"]), num(m["down_cap"])])
        L.append(table(["Index", "Start", "CAGR", "Vol", "Sharpe", "Max DD", "MDD peak→trough",
                        "MDD peak→recovery", "Longest underwater", "Worst yr", "Best yr", "Up-cap",
                        "Down-cap"], rows))
        L.append("")

    L.append("### Calendar-year total returns (2026 = YTD to last close)")
    L.append("")
    rows = []
    for sym in order:
        m = results["full"].get(sym)
        if not m:
            continue
        rows.append([sym] + [pct(m["years"].get(y)) for y in YEARS])
    L.append(table(["Index"] + [str(y) for y in YEARS], rows))
    L.append("")
    L.append("Sharpe uses the 3-month T-bill (FRED DTB3) as the risk-free rate. Drawdown durations are "
             "calendar days. 'Longest underwater' is the longest stretch below a prior high of any drawdown.")
    L.append("")
    return "\n".join(L), {"results": results, "series": series, "bench": bench}


# --------------------------------------------------------------------------------------------- PART 2
def rh_client():
    sys.path.insert(0, str(REPO / "scripts"))
    import rh_direct as r
    return r._Client(r._keychain_token(), 60)


class RH:
    """Cached, read-only wrapper over rh_direct. Refuses any tool not in READ_ONLY_TOOLS."""

    def __init__(self, offline: bool = False):
        self._c = None
        self.offline = offline
        self.calls = 0
        RH_CACHE.mkdir(parents=True, exist_ok=True)

    def call(self, name: str, args: dict) -> dict:
        if name not in READ_ONLY_TOOLS:
            raise RuntimeError(f"refusing non-read-only tool {name}")
        key = hashlib.sha1(json.dumps([name, args], sort_keys=True).encode()).hexdigest()
        p = RH_CACHE / f"{name}_{key[:16]}.json"
        if p.exists():
            return json.loads(p.read_text())
        if self.offline:
            raise RuntimeError(f"offline and not cached: {name} {args}")
        if self._c is None:
            self._c = rh_client()
        for attempt in range(3):
            try:
                out = self._c.call(name, args)
                break
            except Exception as e:  # transient network / rate errors: brief backoff, then re-raise
                if attempt == 2:
                    raise
                time.sleep(2 + 3 * attempt)
                if "auth" in str(e).lower():
                    self._c = rh_client()
        self.calls += 1
        p.write_text(json.dumps(out))
        return out


def rh_daily_closes(rh: RH, sym: str, start: str, end: str | None = None, adj: str = "none") -> pd.Series:
    args = {"symbols": [sym], "start_time": f"{start}T00:00:00Z", "interval": "day",
            "adjustment_type": adj}
    if end:
        args["end_time"] = f"{end}T23:59:00Z"
    out = rh.call("get_equity_historicals", args)
    bars = [b for b in out["data"]["results"][0]["bars"] if not b.get("interpolated")]
    return pd.Series([float(b["close_price"]) for b in bars],
                     index=pd.to_datetime([b["begins_at"][:10] for b in bars]), name=sym)


def rh_daily_bars(rh: RH, sym: str, start: str) -> pd.DataFrame:
    out = rh.call("get_equity_historicals", {"symbols": [sym], "start_time": f"{start}T00:00:00Z",
                                             "interval": "day", "adjustment_type": "none"})
    bars = [b for b in out["data"]["results"][0]["bars"] if not b.get("interpolated")]
    return pd.DataFrame({"open": [float(b["open_price"]) for b in bars],
                         "close": [float(b["close_price"]) for b in bars]},
                        index=pd.to_datetime([b["begins_at"][:10] for b in bars]))


def part2(refresh: bool, rf: pd.Series, rh: RH | None) -> tuple[str, dict]:
    L = ["## PART 2 — Option-income ETFs: total return (distributions reinvested) vs the underlying", ""]
    L.append("Prices + distributions: Yahoo chart API (raw close + dividend events). TR is computed here as "
             "`TR_t = TR_{t-1} x (close_t + dist_t) / close_{t-1}` (distribution reinvested at the ex-date "
             "close, no taxes). Robinhood `get_equity_historicals` returns prices only (no distributions), "
             "so RH is used as an independent check on the closes (column 'RH close check').")
    L.append("")
    rows, notes, res = [], [], {}
    under_cache: dict[str, pd.DataFrame] = {}
    for etf, und in ETFS:
        df = load_yahoo(etf, refresh)
        if df is None:
            rh_hit = ""
            if rh is not None:
                try:
                    srch = rh.call("search", {"query": etf})
                    hits = [x.get("symbol") for x in (srch.get("data") or {}).get("results", [])]
                    rh_hit = f" RH search for '{etf}' returns {hits or 'nothing'}."
                except Exception as e:
                    rh_hit = f" RH search failed: {e}"
            notes.append(f"**{etf}**: no data on Yahoo ('symbol may be delisted').{rh_hit} Excluded; "
                         "the PUT index in Part 1 is the strategy it tracked.")
            continue
        if und not in under_cache:
            under_cache[und] = load_yahoo(und, refresh)
        udf = under_cache[und]
        if df.attrs.get("splits"):
            notes.append(f"{etf}: Yahoo reports splits {df.attrs['splits']} (closes are split-adjusted).")
        start = max(df.index[1], udf.index[1])  # skip listing day
        tr = total_return_index(df)
        utr = total_return_index(udf)
        tr, utr = tr[tr.index >= start], utr[utr.index >= start]
        common = tr.index.intersection(utr.index)
        tr, utr = tr.reindex(common), utr.reindex(common)
        px = df["close"].reindex(common)
        yrs = (common[-1] - common[0]).days / 365.25
        m = metrics(tr, utr, rf)
        um = metrics(utr, None, rf)
        last = common[-1]
        ttm = df["div"][(df.index > last - pd.Timedelta(days=365)) & (df.index <= last)].sum()
        adj = df["adjclose"].reindex(common)
        adj_cagr = (adj.iloc[-1] / adj.iloc[0]) ** (1 / yrs) - 1
        rh_chk = "n/a"
        if rh is not None:
            try:
                # RH daily equity history only reaches back to ~2018-12; compare every common day
                rc = rh_daily_closes(rh, etf, common[0].strftime("%Y-%m-%d"), adj="split")
                j = rc.index.intersection(common)
                diff = (rc.reindex(j) / df["close"].reindex(j) - 1).abs()
                rh_chk = (f"{len(j)}d from {j[0].date()}: median {diff.median() * 100:.2f}%, "
                          f"{(diff > 0.005).sum()}d >0.5%")
            except Exception as e:
                rh_chk = f"RH err: {str(e)[:40]}"
        res[etf] = {"m": m, "um": um, "und": und, "yrs": yrs}
        rows.append([f"{etf} vs {und}", f"{common[0].date()} ({yrs:.1f}y)",
                     pct(m["cagr"]), pct(um["cagr"]), pct(m["total"], 0), pct(um["total"], 0),
                     pct((px.iloc[-1] / px.iloc[0]) ** (1 / yrs) - 1), pct(ttm / df["close"].iloc[-1]),
                     pct(m["vol"]), pct(um["vol"]), pct(m["mdd"]), pct(um["mdd"]),
                     num(m["up_cap"]), num(m["down_cap"]), num(m["sharpe"]), num(um["sharpe"]),
                     f"{pct(adj_cagr, 2)} / {pct(m['cagr'], 2)}", rh_chk])
    L.append(table(["ETF vs underlying", "Window", "ETF TR CAGR", "Und. TR CAGR", "ETF TR total",
                    "Und. TR total", "ETF PRICE-only CAGR", "TTM distrib. yield", "ETF vol", "Und. vol",
                    "ETF max DD", "Und. max DD", "Up-cap", "Down-cap", "ETF Sharpe", "Und. Sharpe",
                    "Yahoo-adj vs our TR CAGR", "RH close check"], rows))
    L.append("")
    L.append("### Calendar-year total returns, ETF vs underlying (partial first year omitted; 2026 = YTD)")
    L.append("")
    yrs_cols = [2021, 2022, 2023, 2024, 2025, 2026]
    rows = []
    for etf, r in res.items():
        rows.append([etf] + [f"{pct(r['m']['years'].get(y), 0)} / {pct(r['um']['years'].get(y), 0)}"
                             for y in yrs_cols])
    L.append(table(["ETF (ETF / underlying)"] + [str(y) for y in yrs_cols], rows))
    L.append("")
    L.append("### Downside episodes (ETF / underlying)")
    L.append("")
    rows = []
    for etf, r in res.items():
        rows.append([etf, f"{pct(r['m']['worst_month'])} / {pct(r['um']['worst_month'])}",
                     f"{r['m']['mdd_peak'].date()}→{r['m']['mdd_trough'].date()}",
                     f"{r['m']['mdd_days']}d" + ("" if r["m"]["mdd_recovered"] else " (not recovered)")])
    L.append(table(["ETF", "Worst month", "ETF max-DD peak→trough", "ETF MDD peak→recovery"], rows))
    L.append("")
    for n in notes:
        L.append(f"- {n}")
    L.append("- Expense ratios are already inside the ETF prices (TR is net of fees); the underlying's TR "
             "is net of its own (small) fee — SPY 0.0945%, QQQ 0.20%, IBIT 0.25%, ETHA 0.25%.")
    L.append("- Survivorship: this list is what still trades. PUTW (WisdomTree put-write) is gone; other "
             "failed/closed option-income ETFs are not in the sample, which flatters the category.")
    L.append("- Distributions are pre-tax. Much of the crypto/high-yield ETFs' payout is return of capital; "
             "TR accounting is agnostic to that, but the headline 'yield' is not income you keep.")
    L.append("")
    return "\n".join(L), res


# --------------------------------------------------------------------------------------------- PART 3
SQRT2 = math.sqrt(2.0)


def ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / SQRT2))


def bs_price(S, K, T, r, sig, kind):
    if T <= 0 or sig <= 0:
        return max(0.0, (K - S) if kind == "put" else (S - K))
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if kind == "call":
        return S * ncdf(d1) - K * math.exp(-r * T) * ncdf(d2)
    return K * math.exp(-r * T) * ncdf(-d2) - S * ncdf(-d1)


def implied_vol_delta(price, S, K, T, r, kind):
    """Invert an OBSERVED option price to IV, then report BS delta. None if price is outside
    no-arbitrage bounds (stale mark)."""
    lo, hi = 0.01, 6.0
    intrinsic = max(0.0, (K * math.exp(-r * T) - S) if kind == "put" else (S - K * math.exp(-r * T)))
    if price <= intrinsic + 1e-4 or price >= (K if kind == "put" else S):
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bs_price(S, K, T, r, mid, kind) > price:
            hi = mid
        else:
            lo = mid
    sig = 0.5 * (lo + hi)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    return sig, (ncdf(d1) - 1) if kind == "put" else ncdf(d1)


class OptionData:
    """Expired-contract lookup + daily bars from RH, cached on disk."""

    def __init__(self, rh: RH, sym: str):
        self.rh, self.sym = rh, sym
        self.ins: dict[tuple[str, str], list[dict]] = {}
        self.bars: dict[str, dict[pd.Timestamp, float]] = {}

    def instruments(self, expiry: str, kind: str) -> list[dict]:
        k = (expiry, kind)
        if k not in self.ins:
            args = {"chain_symbol": self.sym, "type": kind, "expiration_dates": expiry}
            state = "expired" if pd.Timestamp(expiry) < pd.Timestamp.today().normalize() else "active"
            args["state"] = state
            out = self.rh.call("get_option_instruments", args)
            lst = (out.get("data") or {}).get("instruments") or []
            # pull any further pages
            nxt = (out.get("data") or {}).get("next")
            while nxt:
                cur = (urllib.parse.parse_qs(urllib.parse.urlsplit(nxt).query).get("cursor") or [None])[0]
                if not cur:
                    break
                out = self.rh.call("get_option_instruments", {**args, "cursor": cur})
                lst += (out.get("data") or {}).get("instruments") or []
                nxt = (out.get("data") or {}).get("next")
            self.ins[k] = lst
        return self.ins[k]

    def load_bars(self, ins: list[dict]) -> None:
        todo = [i for i in ins if i["id"] not in self.bars]
        for j in range(0, len(todo), 10):
            chunk = todo[j:j + 10]
            exp = chunk[0]["expiration_date"]
            start = (pd.Timestamp(exp) - pd.Timedelta(days=75)).strftime("%Y-%m-%d")
            out = self.rh.call("get_option_historicals", {
                "instrument_ids": [i["id"] for i in chunk], "start_time": f"{start}T00:00:00Z",
                "end_time": f"{exp}T23:59:00Z", "interval": "day"})
            got = {x["instrument_id"]: x["bars"] for x in out["data"]["results"]}
            for i in chunk:
                self.bars[i["id"]] = {pd.Timestamp(b["begins_at"][:10]): float(b["close_price"])
                                      for b in got.get(i["id"], []) if not b.get("interpolated")}


def third_fridays_and_weeklies(d: pd.Timestamp, lo: int, hi: int, target: int) -> list[pd.Timestamp]:
    """Candidate expiry dates (Fridays, plus the Thursday before for holiday weeks) in [lo, hi] DTE,
    nearest-to-target first."""
    cands = []
    for k in range(lo, hi + 1):
        e = d + pd.Timedelta(days=k)
        if e.weekday() == 4:
            cands.append(e)
    return sorted(cands, key=lambda e: abs((e - d).days - target))


def earnings_dates(rh: RH, sym: str, cik: int | None, refresh: bool) -> list[pd.Timestamp]:
    """Union of SEC 8-K item-2.02 filing dates (full history) and RH get_earnings_results (last 8 q +
    the next scheduled one)."""
    out = set()
    if cik:
        body = cached(RAW / f"edgar_{cik}.json", EDGAR_URL.format(cik=cik), refresh, ua=SEC_UA)
        if body:
            f = json.loads(body)["filings"]["recent"]
            for form, items, date in zip(f["form"], f["items"], f["filingDate"]):
                if form == "8-K" and "2.02" in (items or ""):
                    out.add(pd.Timestamp(date))
    try:
        e = rh.call("get_earnings_results", {"symbol": sym})
        for q in (e.get("data") or {}).get("results", []):
            d = (q.get("report") or {}).get("date")
            if d:
                out.add(pd.Timestamp(d))
    except Exception:
        pass
    return sorted(out)


def replay(sym: str, strategy: str, und: pd.DataFrame, od: OptionData, earn: list[pd.Timestamp],
           rf: pd.Series, capital: float, haircut: float, start: str, strict21: bool = False,
           stop: tuple | None = None, fee: float = 0.03) -> dict:
    """Daily event loop over REAL option marks.

    strategy: 'csp'   sell ~0.25-delta put; if assigned, sell the shares next open, restart.
              'wheel' sell ~0.25-delta put; if assigned, sell ~0.30-delta calls with strike >= cost basis
                      (assignment strike - put credit) until called away, then back to puts.
              'cc'    hold 100-share lots; sell ~0.30-delta OTM calls; if called away, rebuy next open.
    Rules (all strategies): 30-45 DTE entry (target 38), close at 50% of credit, at 21 DTE close if OTM
    (ITM is held to expiry and assigned — strict21=True closes regardless), skip any expiry with an
    earnings date inside, min credit $0.05. Fills: sell at mark*(1-h), buy at mark*(1+h), rounded to the
    cent against us, plus `fee` per contract per side. Settlement at expiry uses the underlying close.

    stop (short puts only): ('mult', m)  buy back when the put's mark >= m x the credit received
                            ('em', k)    buy back when the underlying closes below strike - k x the
                                         expected move at entry (IV x sqrt(DTE/365) x spot)
                            ('sma', n)   buy back when the underlying closes below its n-day SMA,
                                         having closed at/above it the previous day (a fresh cross-down
                                         during the trade — otherwise every entry below the SMA would
                                         be stopped the next morning)"""
    days = und.index[und.index >= start]
    sma = und["close"].rolling(stop[1]).mean() if stop and stop[0] == "sma" else None
    cash, shares, basis = capital, 0, None
    pos = None                      # short option dict
    pending_sell_shares = pending_buy_shares = False
    pending_trade = None            # CSP-only: assigned put whose P&L closes when the shares are sold
    next_try = days[0]
    eq, log, trades = [], [], []
    stats = {"opens": 0, "tp": 0, "close21": 0, "stop": 0, "expired_otm": 0, "assigned": 0, "called": 0,
             "wins": 0, "losses": 0, "opt_pnl": 0.0, "days_short": 0, "earn_skips": 0,
             "no_cand": 0, "credits": 0.0}

    def sell_px(p):
        return max(0.01, math.floor(p * (1 - haircut) * 100) / 100)

    def buy_px(p):
        return math.ceil(p * (1 + haircut) * 100 - 1e-9) / 100

    def rate(d):
        v = rf.asof(d)
        return 0.04 if v is None or math.isnan(v) else float(v)

    def try_open(d, kind):
        S = und.loc[d, "close"]
        for e in third_fridays_and_weeklies(d, 30, 45, 38):
            if any(d < x <= e for x in earn):
                stats["earn_skips"] += 1
                continue
            ins = od.instruments(e.strftime("%Y-%m-%d"), kind)
            if not ins:
                alt = e - pd.Timedelta(days=1)   # holiday-week Thursday expiry
                ins = od.instruments(alt.strftime("%Y-%m-%d"), kind)
                if not ins:
                    continue
                e = alt
            T = (e - d).days / 365.0
            n = int(shares // 100) if kind == "call" else None
            if kind == "put":
                pool = [i for i in ins if 0.55 * S <= float(i["strike_price"]) <= S]
                pool = sorted(pool, key=lambda i: abs(float(i["strike_price"]) - 0.88 * S))[:20]
                target = -0.25
            else:
                floor_k = max(S, basis) if strategy == "wheel" and basis else S
                pool = [i for i in ins if floor_k <= float(i["strike_price"]) <= max(1.6 * S, floor_k * 1.3)]
                pool = sorted(pool, key=lambda i: abs(float(i["strike_price"]) - max(1.08 * S, floor_k)))[:20]
                target = 0.30
            if not pool:
                continue
            od.load_bars(pool)
            best = None
            for i in pool:
                p = od.bars[i["id"]].get(d)
                if p is None:
                    continue
                K = float(i["strike_price"])
                iv = implied_vol_delta(p, S, K, T, rate(d), kind)
                if iv is None:
                    continue
                if kind == "put":
                    cnt = int(cash // (100 * K))
                    if cnt < 1:
                        continue
                else:
                    cnt = n
                score = abs(iv[1] - target)
                if best is None or score < best[0]:
                    best = (score, i, p, K, iv, cnt)
            if best is None:
                continue
            _, i, p, K, iv, cnt = best
            if kind == "put" and abs(iv[1]) < 0.10:
                continue   # can only afford junk-delta strikes
            credit = sell_px(p)
            if credit < 0.05:
                continue
            return {"id": i["id"], "kind": kind, "K": K, "exp": e, "credit": credit, "n": cnt,
                    "open": d, "last": p, "delta": iv[1], "iv": iv[0], "S0": S,
                    "em": iv[0] * math.sqrt((e - d).days / 365.0) * S}
        return None

    def baseline_outcome(p0, d0):
        """Counterfactual for a stopped put: keep running it under the no-stop rules from the day after
        the stop. Returns P&L per the same accounting (incl. assignment + sale at next open)."""
        after = und.index[und.index > d0]
        for k, d in enumerate(after):
            S = und.loc[d, "close"]
            if d >= p0["exp"]:
                if S < p0["K"]:
                    nxt = after[k + 1] if k + 1 < len(after) else d
                    sale = und.loc[nxt, "open"] if k + 1 < len(after) else S
                    return (p0["credit"] - (p0["K"] - sale)) * 100 * p0["n"] - fee * p0["n"]
                return p0["credit"] * 100 * p0["n"] - fee * p0["n"]
            p = od.bars[p0["id"]].get(d)
            if p is None:
                continue
            cost = buy_px(p)
            if cost <= 0.5 * p0["credit"] or ((p0["exp"] - d).days <= 21 and S > p0["K"]):
                return (p0["credit"] - cost) * 100 * p0["n"] - 2 * fee * p0["n"]
        return None  # still open at the end of data

    def close_trade(pos, d, pnl, how, extra=None):
        t = {"open": pos["open"], "close": d, "kind": pos["kind"], "K": pos["K"], "n": pos["n"],
             "credit": pos["credit"], "exit": how, "pnl": pnl, "collateral": 100 * pos["K"] * pos["n"],
             "iv_entry": pos["iv"], "S0": pos["S0"]}
        if extra:
            t.update(extra)
        trades.append(t)

    prev_close = None
    for d in days:
        S, O = und.loc[d, "close"], und.loc[d, "open"]
        if pending_sell_shares and shares:
            cash += shares * O
            log.append((d, f"sell {shares} sh @ {O:.2f} (assigned lot)"))
            if pending_trade:
                pt = pending_trade
                pt["pnl"] += (O - pt["K"]) * shares
                trades.append(pt)
                pending_trade = None
            shares, basis, pending_sell_shares = 0, None, False
        if pending_buy_shares:
            n = int(cash // (100 * O)) * 100
            if n:
                cash -= n * O
                shares, basis = n, O
                log.append((d, f"buy {n} sh @ {O:.2f}"))
            pending_buy_shares = False
        if strategy == "cc" and d == days[0]:
            n = int(cash // (100 * S)) * 100
            cash -= n * S
            shares, basis = n, S

        if pos:
            stats["days_short"] += 1
            p = od.bars[pos["id"]].get(d)
            if p is not None:
                pos["last"] = p
            if d >= pos["exp"]:
                K, n = pos["K"], pos["n"]
                itm = S < K if pos["kind"] == "put" else S > K
                intrinsic = max(0.0, (K - S) if pos["kind"] == "put" else (S - K))
                pnl = (pos["credit"] - intrinsic) * 100 * n - fee * n
                stats["opt_pnl"] += pnl
                stats["wins" if pnl > 0 else "losses"] += 1
                if itm and pos["kind"] == "put":
                    cash -= 100 * n * K
                    shares += 100 * n
                    basis = K - pos["credit"]
                    stats["assigned"] += 1
                    log.append((d, f"ASSIGNED {n}x {K}P, S={S:.2f}, basis {basis:.2f}"))
                    if strategy == "csp":
                        pending_sell_shares = True
                        pending_trade = {"open": pos["open"], "close": d, "kind": "put", "K": K, "n": n,
                                         "credit": pos["credit"], "exit": "assigned",
                                         "pnl": pos["credit"] * 100 * n - fee * n,
                                         "collateral": 100 * K * n, "iv_entry": pos["iv"], "S0": pos["S0"]}
                    else:
                        close_trade(pos, d, pnl, "assigned")
                elif itm and pos["kind"] == "call":
                    cash += 100 * n * K
                    shares -= 100 * n
                    stats["called"] += 1
                    log.append((d, f"CALLED AWAY {n}x {K}C, S={S:.2f}"))
                    close_trade(pos, d, pnl, "called")
                    if strategy == "cc":
                        pending_buy_shares = True
                    basis = None if shares == 0 else basis
                else:
                    stats["expired_otm"] += 1
                    close_trade(pos, d, pnl, "expired")
                pos = None
                next_try = d + pd.Timedelta(days=1)
            elif p is not None:
                cost = buy_px(p)
                dte = (pos["exp"] - d).days
                otm = S > pos["K"] if pos["kind"] == "put" else S < pos["K"]
                reason = None
                if pos["kind"] == "put" and stop:
                    if stop[0] == "mult" and p >= stop[1] * pos["credit"]:
                        reason = "stop"
                    elif stop[0] == "em" and S < pos["K"] - stop[1] * pos["em"]:
                        reason = "stop"
                    elif (stop[0] == "sma" and sma is not None and not math.isnan(sma.loc[d])
                          and S < sma.loc[d] and prev_close is not None
                          and prev_close >= sma.shift(1).loc[d]):
                        reason = "stop"
                if reason is None and cost <= 0.5 * pos["credit"]:
                    reason = "tp"
                elif reason is None and dte <= 21 and (otm or strict21):
                    reason = "close21"
                if reason:
                    pnl = (pos["credit"] - cost) * 100 * pos["n"] - 2 * fee * pos["n"]
                    cash -= cost * 100 * pos["n"] + fee * pos["n"]
                    stats["opt_pnl"] += pnl
                    stats[reason] += 1
                    stats["wins" if pnl > 0 else "losses"] += 1
                    extra = None
                    if reason == "stop":
                        ivs = implied_vol_delta(p, S, pos["K"], max(dte, 1) / 365.0, rate(d), "put")
                        cf = baseline_outcome(pos, d)
                        extra = {"iv_stop": ivs[0] if ivs else None, "cf_pnl": cf}
                    close_trade(pos, d, pnl, reason, extra)
                    log.append((d, f"close {pos['n']}x {pos['K']}{pos['kind'][0].upper()} @ {cost:.2f} "
                                   f"({reason}, credit {pos['credit']:.2f}, S={S:.2f})"))
                    pos = None
                    next_try = d + pd.Timedelta(days=1)

        if pos is None and d >= next_try and not pending_sell_shares and not pending_buy_shares:
            kind = "call" if (strategy in ("wheel", "cc") and shares >= 100) else (
                "put" if strategy in ("csp", "wheel") else None)
            if kind:
                new = try_open(d, kind)
                if new:
                    pos = new
                    cash += new["credit"] * 100 * new["n"] - fee * new["n"]
                    stats["opens"] += 1
                    stats["credits"] += new["credit"] * 100 * new["n"]
                    log.append((d, f"SELL {new['n']}x {new['K']}{kind[0].upper()} {new['exp'].date()} "
                                   f"@ {new['credit']:.2f} (delta {new['delta']:+.2f}, iv {new['iv']:.0%}, S={S:.2f})"))
                else:
                    stats["no_cand"] += 1
                    # blocked (earnings / nothing affordable / sub-5c credit): retry after a week
                    next_try = d + pd.Timedelta(days=7)
        liab = (pos["last"] * 100 * pos["n"]) if pos else 0.0
        eq.append((d, cash + shares * S - liab))
        prev_close = S

    curve = pd.Series([v for _, v in eq], index=[d for d, _ in eq])
    return {"curve": curve, "stats": stats, "log": log, "open_pos": pos, "trades": trades}


def part3(rf: pd.Series, rh: RH, capital: float, haircut: float, refresh: bool) -> tuple[str, dict]:
    L = ["## PART 3 — Single-stock wheel: feasibility of a replay on Robinhood historical option data", ""]
    # ---- feasibility probe (cached calls; documents what exists)
    probe_rows = []
    for sym, exps in (("SOFI", ["2021-09-17", "2021-12-17", "2022-12-16", "2024-03-15", "2025-06-20"]),
                      ("F", ["2016-06-17", "2017-06-16", "2018-06-15", "2019-06-21", "2025-06-20"]),
                      ("IBIT", ["2024-11-22", "2024-12-20", "2025-06-20"])):
        od = OptionData(rh, sym)
        for exp in exps:
            ins = od.instruments(exp, "put")
            if not ins:
                probe_rows.append([sym, exp, 0, "—", "—", "—"])
                continue
            S = rh_daily_closes(rh, sym, (pd.Timestamp(exp) - pd.Timedelta(days=45)).strftime("%Y-%m-%d"),
                                (pd.Timestamp(exp) - pd.Timedelta(days=35)).strftime("%Y-%m-%d"))
            s0 = float(S.iloc[-1]) if len(S) else None
            near = sorted(ins, key=lambda i: abs(float(i["strike_price"]) - 0.9 * (s0 or 0)))[:4]
            od.load_bars(near)
            e = pd.Timestamp(exp)
            cov = []
            for i in near:
                b = od.bars[i["id"]]
                in_win = [d for d in b if 30 <= (e - d).days <= 45]
                cov.append(f"{float(i['strike_price']):g}P: {len(b)} real bars, {len(in_win)} in 30-45 DTE"
                           + (f", first {min(b).date()}" if b else ""))
            probe_rows.append([sym, exp, len(ins), f"{s0:.2f}" if s0 else "n/a", "; ".join(cov),
                               "day (intraday = mostly interpolated)"])
    L.append("### Probe: what RH returns for expired contracts")
    L.append("")
    L.append(table(["Underlying", "Expiry", "# put contracts", "Spot ~40 DTE", "Strikes near 0.9x spot",
                    "Usable granularity"], probe_rows))
    L.append("")
    L.append("Findings from the probe (and from the raw responses cached in data/bakeoff/rh_cache/):")
    L.append("- `get_option_instruments(state='expired')` returns expired contracts back to at least 2018 "
             "for F (none for the 2016/2017 June expiries tested), SOFI from its 2021 listing, IBIT from "
             "its Nov-2024 options listing.")
    L.append("- `get_option_historicals` bars carry only `begins_at, open/high/low/close_price, session, "
             "interpolated` — **no bid/ask, no volume, no open interest, no IV, no greeks**. Delta/IV must "
             "be inferred by inverting the observed price (done here with Black-Scholes, no dividends, "
             "T-bill rate). RH does not document whether the daily close is last trade or mark; every "
             "standard strike has a non-interpolated bar every day even far OTM, which looks like a "
             "mark/mid series, so fills are modelled as mark minus/plus a haircut.")
    L.append("- Only DAILY bars are real history: hourly/10-minute requests for old contracts come back "
             "~90% `interpolated: true` (only the final days are real). Newer strikes added mid-cycle "
             "have bars only from their listing date.")
    L.append("- The bar on expiration day itself is sometimes missing, so settlement uses the underlying's "
             "close vs strike.")
    L.append("- Historical earnings dates: RH `get_earnings_results` covers only the trailing 8 quarters; "
             "older ones come from SEC EDGAR 8-K item 2.02 filing dates (they match RH on the overlap).")
    L.append("")
    L.append("**Verdict: FEASIBLE at daily granularity** for 30-45 DTE, ~0.25-delta puts on SOFI/F/IBIT, "
             "with two limitations baked into every number below: (1) no historical bid/ask, so the "
             "spread is an assumption (`--haircut`), and (2) delta is implied from the mark, not a "
             "historical greek feed.")
    L.append("")

    # ---- the replay
    und_cfg = [("IBIT", None, "2024-11-20"), ("SOFI", 1818874, "2021-07-01")]
    res = {}
    for sym, cik, start in und_cfg:
        # pull ~120 extra calendar days so the 50-day SMA stop is defined from the first trade
        und = rh_daily_bars(rh, sym, (pd.Timestamp(start) - pd.Timedelta(days=120)).strftime("%Y-%m-%d"))
        earn = earnings_dates(rh, sym, cik, refresh)
        od = OptionData(rh, sym)
        uc = und["close"][und.index >= start]
        bh = capital * uc / uc.iloc[0]
        res[sym] = {"bh": bh, "earn": earn, "und": und, "od": od}
        for strat in ("csp", "wheel", "cc"):
            for h in sorted({haircut, 0.0}):
                print(f"  replay {sym} {strat} haircut={h}", file=sys.stderr)
                res[sym][(strat, h)] = replay(sym, strat, und, od, earn, rf, capital, h, start)
        print(f"  replay {sym} csp strict-21", file=sys.stderr)
        res[sym][("csp-strict21", haircut)] = replay(sym, "csp", und, od, earn, rf, capital, haircut,
                                                     start, strict21=True)

    L.append(f"### Replay results — starting capital ${capital:,.0f}, fills at mark ∓ {haircut:.0%} "
             "(and at mark, 0% haircut, for comparison), $0.03/contract/side fees")
    L.append("")
    L.append("Rules: sell 30-45 DTE (target 38) options; puts at the strike whose implied delta is "
             "nearest -0.25, calls nearest +0.30 (wheel calls: strike >= cost basis = assignment strike - "
             "put credit); contracts = floor(cash / (100 x strike)) so every put is fully cash-secured; "
             "buy back at 50% of credit; at 21 DTE buy back if OTM, if ITM hold to expiry and take "
             "assignment (the wheel needs assignment to exist); skip any expiry with an earnings date "
             "inside (retry in 7 days); minimum credit $0.05. CSP-only sells assigned shares at the next "
             "open. Covered call ('cc') starts fully invested in 100-share lots and re-buys at the next "
             "open if called away. Open positions at the end are marked to the last option mark.")
    L.append("")
    rows = []
    for sym, _, start in und_cfg:
        bh = res[sym]["bh"]
        bm = metrics(bh, None, rf)
        rows.append([sym, "buy & hold", "—", pct(bm["total"], 0), pct(bm["cagr"]), pct(bm["mdd"]),
                     pct(bm["vol"]), "1.00 / 1.00", "—", "—", "—", "—", f"{bm['start']}→{bm['end']}"])
        for key in [("csp", haircut), ("csp", 0.0), ("csp-strict21", haircut), ("wheel", haircut),
                    ("wheel", 0.0), ("cc", haircut), ("cc", 0.0)]:
            if key not in res[sym]:
                continue
            r = res[sym][key]
            m = metrics(r["curve"], bh, rf)
            st = r["stats"]
            ntr = st["wins"] + st["losses"]
            rows.append([sym, key[0], f"{key[1]:.0%}", pct(m["total"], 0), pct(m["cagr"]), pct(m["mdd"]),
                         pct(m["vol"]), f"{num(m['up_cap'])} / {num(m['down_cap'])}",
                         f"{st['opens']} ({st['tp']} TP, {st['close21']} @21d, {st['expired_otm']} exp OTM)",
                         f"{st['assigned']} / {st['called']}",
                         f"{st['wins']}/{ntr}" if ntr else "0/0",
                         f"{st['days_short'] / len(r['curve']):.0%}",
                         f"${st['opt_pnl']:,.0f} opt P&L"])
    L.append(table(["Und.", "Strategy", "Haircut", "Total return", "CAGR", "Max DD", "Vol",
                    "Up/Down capture vs B&H", "Options sold (exits)", "Assigned / called away",
                    "Winning option trades", "% days with a short option", "Notes"], rows))
    L.append("")
    L.append("'Opt P&L' is the sum of closed short-option P&L (the 'income' a promoter would quote); the "
             "total-return column includes the stock P&L on assigned shares, which is where the wheel's "
             "losses hide.")
    L.append("")
    for sym, _, _ in und_cfg:
        L.append(f"<details><summary>{sym} wheel trade log ({haircut:.0%} haircut)</summary>")
        L.append("")
        L.append("```")
        for d, msg in res[sym][("wheel", haircut)]["log"]:
            L.append(f"{d.date()}  {msg}")
        L.append("```")
        L.append("</details>")
        L.append("")
        L.append(f"{sym} earnings dates used ({len(res[sym]['earn'])}): "
                 + ", ".join(str(x.date()) for x in res[sym]["earn"]))
        L.append("")
    L.append(stop_section(res, und_cfg, rf, capital, haircut))
    L.append(f"RH calls made this run (uncached): {rh.calls}.")
    L.append("")
    return "\n".join(L), res


STOP_RULES = [("none", None), ("mark >= 2x credit", ("mult", 2.0)), ("mark >= 3x credit", ("mult", 3.0)),
              ("S < K - 1x exp. move", ("em", 1.0)), ("S crosses below 50d SMA", ("sma", 50))]


def stop_section(res: dict, und_cfg: list, rf: pd.Series, capital: float, haircut: float) -> str:
    """Owner rule: a stop-loss must always be a rule. Test the candidate stops on CSP-only (the
    structure where a stop is a meaningful choice) and measure what each costs in whipsaws vs saves in
    tails. A stop 'whipsaws' when the same put, left alone under the no-stop rules, would have closed
    for a profit (50% TP / 21-DTE OTM close / expired worthless)."""
    L = ["### Stop-loss rules on cash-secured puts (CSP-only, same entries/exits as above + the stop)", ""]
    L.append("Stop fills use that day's closing mark + the same haircut. 'Return on collateral (ann.)' = "
             "sum of trade P&L / sum of (collateral x days held / 365) — the yield while capital is "
             "actually at risk. Worst trade includes the loss on assigned shares sold at the next open. "
             "Whipsaw rate = % of stop-outs whose counterfactual (same put, no stop, normal rules) ended "
             "profitable. 'Stops vs no-stop $' = sum over stop-outs of (stop P&L - counterfactual P&L): "
             "negative means the stops cost money on the trades they touched. IV at stop / IV at entry "
             "tests the 'you buy back at peak implied vol' warning.")
    L.append("")
    rows, detail = [], []
    for sym, _, start in und_cfg:
        r0 = res[sym]
        uc = r0["bh"]
        dd = uc / uc.cummax() - 1
        tr = dd.idxmin()
        pk = uc.loc[:tr].idxmax()
        for name, rule in STOP_RULES:
            r = replay(sym, "csp", r0["und"], r0["od"], r0["earn"], rf, capital, haircut, start, stop=rule)
            res[sym][("stop", name)] = r
            m = metrics(r["curve"], r0["bh"], rf)
            t = [x for x in r["trades"] if x["kind"] == "put"]
            coll_days = sum(x["collateral"] * max((x["close"] - x["open"]).days, 1) / 365 for x in t)
            roc = sum(x["pnl"] for x in t) / coll_days if coll_days else float("nan")
            worst = min((x["pnl"] / x["collateral"] for x in t), default=float("nan"))
            stops = [x for x in t if x["exit"] == "stop"]
            cf = [x for x in stops if x.get("cf_pnl") is not None]
            whip = sum(1 for x in cf if x["cf_pnl"] > 0) / len(cf) if cf else float("nan")
            saved = sum(x["pnl"] - x["cf_pnl"] for x in cf) if cf else 0.0
            ivr = [x["iv_stop"] / x["iv_entry"] for x in stops if x.get("iv_stop")]
            crash = [x for x in t if pk <= x["close"] <= tr + pd.Timedelta(days=5)]
            eq_crash = r["curve"].asof(tr) / r["curve"].asof(pk) - 1
            rows.append([sym, name, pct(m["total"], 0), pct(m["cagr"]), pct(roc), pct(m["mdd"]),
                         pct(worst), f"{len(stops)} / {len(t)}",
                         f"{whip:.0%}" if cf else "—", f"${saved:,.0f}" if cf else "—",
                         f"{np.median(ivr):.2f}x" if ivr else "—",
                         f"{pct(eq_crash)} (B&H {pct(uc.asof(tr) / uc.asof(pk) - 1)}); "
                         f"{len(crash)} trades closed, ${sum(x['pnl'] for x in crash):,.0f}"])
            for x in stops:
                detail.append(f"{sym} {name:24s} open {x['open'].date()} stop {x['close'].date()} "
                              f"K={x['K']:g} x{x['n']} credit {x['credit']:.2f} pnl ${x['pnl']:,.0f} "
                              f"cf ${x['cf_pnl'] if x.get('cf_pnl') is not None else float('nan'):,.0f} "
                              f"IV {x['iv_entry']:.0%}->{(x.get('iv_stop') or float('nan')):.0%}")
        L.append(f"{sym} worst underlying drawdown in the replay window: {pk.date()} → {tr.date()} "
                 f"({pct(dd.min())}).")
        L.append("")
    L.append(table(["Und.", "Stop rule", "Total return", "CAGR", "Return on collateral (ann.)", "Max DD",
                    "Worst trade (% collateral)", "Stop-outs / put trades", "Whipsaw rate",
                    "Stops vs no-stop $", "Median IV at stop / IV at entry",
                    "During the underlying's max drawdown: equity (B&H); trades closed, P&L"], rows))
    L.append("")
    L.append("<details><summary>Every stop-out (P&L vs counterfactual, IV entry→stop)</summary>")
    L.append("")
    L.append("```")
    L += detail
    L.append("```")
    L.append("</details>")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parts", default="1,2,3", help="comma list of parts to run (default 1,2,3)")
    ap.add_argument("--refresh", action="store_true", help="re-download Cboe/Yahoo/FRED/EDGAR data")
    ap.add_argument("--offline", action="store_true", help="RH: use cache only, never call the MCP")
    ap.add_argument("--capital", type=float, default=10_000.0, help="wheel replay starting capital")
    ap.add_argument("--haircut", type=float, default=0.05,
                    help="replay fill haircut: sell at mark*(1-h), buy at mark*(1+h) (default 0.05)")
    ap.add_argument("--out", default=str(REPORT))
    args = ap.parse_args()
    parts = {p.strip() for p in args.parts.split(",")}
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    rf = load_rf(args.refresh)
    rh = None
    if parts & {"2", "3"}:
        try:
            rh = RH(offline=args.offline)
        except Exception as e:
            print(f"RH unavailable: {e}", file=sys.stderr)

    sections = [f"# Options-income bake-off — generated {dt.datetime.now():%Y-%m-%d %H:%M} by "
                "scripts/bakeoff_options.py", "",
                "Read-only research. No orders were reviewed or placed. All figures are computed from "
                "the cached raw data in data/bakeoff/ — nothing is typed in by hand.", ""]
    if "1" in parts:
        print("PART 1 ...", file=sys.stderr)
        sections.append(part1(args.refresh, rf)[0])
    if "2" in parts:
        print("PART 2 ...", file=sys.stderr)
        sections.append(part2(args.refresh, rf, rh)[0])
    if "3" in parts and rh is not None:
        print("PART 3 ...", file=sys.stderr)
        sections.append(part3(rf, rh, args.capital, args.haircut, args.refresh)[0])
    sections.append("""## Caveats that apply to every number above

- **Index != what you can trade.** Cboe indexes use SPX options: European, cash-settled, no early
  assignment, no share delivery, one diversified underlying, institutional-size spreads, gross of
  costs and taxes. One SPX contract is ~$776k notional; the retail equivalent is SPY (~$66k per CSP)
  or single names. Single-stock CSPs add gap/earnings/idiosyncratic risk the indexes never see.
- **PUT's CDN file is daily only from 2007-01-03** (earlier rows are sparse), so its 'full history'
  equals its since-2007 row. CLL starts 2008-08-26, days before the Lehman crash, so its max-DD
  window is a start-date artifact.
- **ETF numbers are net of fees but pre-tax**; distributions are mostly ordinary income / return of
  capital. Survivorship: closed funds (e.g. PUTW) are missing, which flatters the category. Crypto
  income ETFs have < 3 years of history — one BTC regime.
- **Replay limits:** RH gives no historical bid/ask, IV or greeks — delta is implied from the daily
  mark; fills are mark -/+ a stated haircut; daily bars mean stops/TPs fire on closes only (no
  intraday fills); idle cash earns nothing here (the Cboe PUT index earns T-bills — at 2024-26
  rates ~4%/yr on idle collateral would be added); one start date per underlying, n = 2 underlyings,
  both chosen with hindsight as 'popular wheel names' — this is a sanity check, not proof of edge.
- **Earnings filter** uses SEC 8-K item 2.02 filing dates (+ RH trailing 8 quarters); IBIT has none.
""")
    Path(args.out).write_text("\n".join(sections))
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
