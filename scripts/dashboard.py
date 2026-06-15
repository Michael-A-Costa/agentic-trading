#!/usr/bin/env python3
"""dashboard.py — a single self-contained HTML dashboard for the agentic book.

Mirrors the visual language of the work "per-dev velocity" dashboard (dark theme, Chart.js,
sortable tables, fold-out "run it yourself" blocks) but plots OUR trades/stocks/pnl instead:

  • headline stat tiles      — account value, exposure, realized + unrealized P&L, win-rate
  • equity curve             — cumulative realized P&L over every closed chunk
  • daily realized P&L       — one bar per ET trading day, green/red
  • per-symbol net realized   — every name traded, ranked
  • exit-type breakdown      — how positions actually closed (stop / TP / EOD / discretionary)
  • open positions           — live unrealized P&L from the snapshot, sortable
  • round-trips              — every FIFO-paired entry→exit, fully sortable

Reads (all offline except the snapshot, which is pre-captured from the MCP):
    data/trades.jsonl              — the executed-fill ledger (source of truth for P&L)
    data/dashboard_snapshot.json   — live portfolio + positions + quotes (regenerate via MCP)

Usage:
    python3 scripts/dashboard.py                       # -> data/dashboard.html (live book)
    python3 scripts/dashboard.py --mode paper          # paper book instead
    python3 scripts/dashboard.py --out /tmp/x.html
"""
from __future__ import annotations

import argparse
import json
from collections import OrderedDict, defaultdict
from datetime import datetime
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trade_ledger import load_rows, build_round_trips  # paper FIFO (paper sells always have a price)
from trade_log import EXIT_LABEL
import reconcile_ledger  # broker-truth realized for the LIVE book

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "data" / "trades.jsonl"
COSTS = REPO / "data" / "costs.jsonl"
TRUTH = REPO / "data" / "ledger_truth.json"
SNAPSHOT = REPO / "data" / "dashboard_snapshot.json"
OUT = REPO / "data" / "dashboard.html"

# Momentum themes the engine has been crowding into — used for the concentration panel.
THEMES = {
    "QBTS": "Quantum", "RGTI": "Quantum", "ARQQ": "Quantum", "QNT": "Quantum",
    "OUST": "Lidar/Sensors", "AEHR": "Semis", "ELVR": "Biotech", "NTLA": "Biotech",
    "TGTX": "Biotech", "HL": "Metals", "AU": "Metals", "HMY": "Metals", "SVM": "Metals",
    "CDE": "Metals", "MOS": "Materials",
}


def theme_of(sym: str) -> str:
    return THEMES.get(sym.upper(), "Other")

EXIT_ORDER = ["take_profit", "scale_out", "winddown", "stop", "eod_flatten", "time_stop", "other", "test"]


def fmt_usd(x: float, cents: bool = True) -> str:
    s = f"{abs(x):,.2f}" if cents else f"{abs(x):,.0f}"
    return f"{'-' if x < 0 else ''}${s}"


def compute(mode: str) -> dict:
    rows = load_rows(LEDGER, since=None, symbol=None, mode=mode)
    trips, _ = build_round_trips(rows)

    # equity curve: cumulative realized over closed chunks, in time order
    trips_sorted = sorted(trips, key=lambda t: t["exit_ts"])
    curve, cum = [], 0.0
    for t in trips_sorted:
        cum += t["realized_usd"]
        curve.append({"ts": t["exit_ts"], "cum": round(cum, 2), "sym": t["symbol"],
                      "pnl": t["realized_usd"]})

    # daily realized
    daily = OrderedDict()
    for t in trips_sorted:
        day = t["exit_ts"][:10]
        daily[day] = round(daily.get(day, 0.0) + t["realized_usd"], 2)

    # per-symbol net realized
    per_sym = defaultdict(float)
    for t in trips:
        per_sym[t["symbol"]] += t["realized_usd"]
    per_sym = sorted(({"sym": k, "pnl": round(v, 2)} for k, v in per_sym.items()),
                     key=lambda d: d["pnl"])

    # exit-type breakdown
    ex = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
    for t in trips:
        e = t["exit_type"] or "other"
        ex[e]["n"] += 1
        ex[e]["pnl"] += t["realized_usd"]
        if t["realized_usd"] > 1e-9:
            ex[e]["wins"] += 1
    exit_rows = []
    for e in EXIT_ORDER + [k for k in ex if k not in EXIT_ORDER]:
        if e not in ex:
            continue
        d = ex[e]
        exit_rows.append({"key": e, "label": EXIT_LABEL.get(e, e), "n": d["n"],
                          "pnl": round(d["pnl"], 2),
                          "winpct": round(100 * d["wins"] / d["n"]) if d["n"] else 0})

    realized = round(sum(t["realized_usd"] for t in trips), 2)
    wins = [t for t in trips if t["realized_usd"] > 1e-9]
    losses = [t for t in trips if t["realized_usd"] < -1e-9]
    gross_w = sum(t["realized_usd"] for t in wins)
    gross_l = -sum(t["realized_usd"] for t in losses)
    stats = {
        "n_trips": len(trips), "n_buys": sum(1 for r in rows if r.get("side") == "buy"),
        "realized": realized,
        "winrate": round(100 * len(wins) / len(trips)) if trips else 0,
        "n_wins": len(wins), "n_losses": len(losses),
        "avg_win": round(gross_w / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_l / len(losses), 2) if losses else 0.0,
        "pf": round(gross_w / gross_l, 2) if gross_l > 1e-9 else None,
        "best": max(trips, key=lambda t: t["realized_usd"]) if trips else None,
        "worst": min(trips, key=lambda t: t["realized_usd"]) if trips else None,
    }

    return {"trips": sorted(trips, key=lambda t: t["exit_ts"], reverse=True),
            "curve": curve, "daily": list(daily.items()),
            "per_sym": per_sym, "exit_rows": exit_rows, "stats": stats}


def load_truth() -> dict:
    """Broker-truth realized for the LIVE book. Tries a fresh reconcile (direct MCP, no LLM); on any
    broker/auth failure falls back to the last-written data/ledger_truth.json so the dashboard still
    builds offline. Raises only if neither path yields data."""
    try:
        d = reconcile_ledger.reconcile()
        TRUTH.write_text(json.dumps(d, indent=2))  # keep the cache fresh as a side effect
        return d
    except Exception as e:  # noqa: BLE001 — broker down / token stale: fall back to cache
        print(f"[dashboard] fresh reconcile failed ({e}); using cached {TRUTH.name}", file=sys.stderr)
        if TRUTH.exists():
            return json.loads(TRUTH.read_text())
        raise SystemExit(f"no broker truth: reconcile failed and no {TRUTH} cache. "
                         "Run: python3 scripts/reconcile_ledger.py --write")


def truth_to_display(truth: dict) -> dict:
    """Map reconcile_ledger's broker-truth dict into the shape render() consumes (same keys as
    compute()), so the LIVE realized side is broker-confirmed, not FIFO-over-our-event-log."""
    trips = truth.get("round_trips", [])
    ts = sorted(trips, key=lambda t: t["exit_ts"])
    curve, cum = [], 0.0
    for t in ts:
        cum += t["realized_usd"]
        curve.append({"ts": t["exit_ts"], "cum": round(cum, 2), "sym": t["symbol"],
                      "pnl": t["realized_usd"]})
    daily = OrderedDict()
    for t in ts:
        day = t["exit_ts"][:10]
        daily[day] = round(daily.get(day, 0.0) + t["realized_usd"], 2)
    best = max(trips, key=lambda t: t["realized_usd"]) if trips else None
    worst = min(trips, key=lambda t: t["realized_usd"]) if trips else None
    stats = {
        "n_trips": truth["n_round_trips"], "n_buys": truth.get("n_orders_filled", 0),
        "realized": truth["realized_usd"], "winrate": truth["win_rate"],
        "n_wins": truth["n_wins"], "n_losses": truth["n_losses"],
        "avg_win": truth["avg_win"], "avg_loss": truth["avg_loss"], "pf": truth["profit_factor"],
        "best": best, "worst": worst,
    }
    exit_rows = [{"key": r["key"], "label": r["label"], "n": r["n"],
                  "pnl": r["realized_usd"], "winpct": r["win_pct"]} for r in truth["exit_types"]]
    return {
        "trips": [{**t, "mode": "live"} for t in sorted(trips, key=lambda t: t["exit_ts"], reverse=True)],
        "curve": curve, "daily": list(daily.items()),
        "per_sym": [{"sym": r["symbol"], "pnl": r["realized_usd"]} for r in truth["per_symbol"]],
        "exit_rows": exit_rows, "stats": stats,
        "drift": truth.get("drift", []), "source": truth.get("source", "broker"),
    }


def total_cost(mode: str) -> dict:
    """Sum LLM spend (DD + relay) from data/costs.jsonl, filtered to `mode`."""
    dd = relay = 0.0
    n = 0
    if COSTS.exists():
        for line in COSTS.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if mode != "all" and r.get("mode") != mode:
                continue
            dd += float(r.get("dd_cost_usd") or 0.0)
            relay += float(r.get("relay_cost_usd") or 0.0)
            n += 1
    return {"dd": round(dd, 2), "relay": round(relay, 2), "total": round(dd + relay, 2), "ticks": n}


def load_snapshot() -> dict | None:
    if not SNAPSHOT.exists():
        return None
    snap = json.loads(SNAPSHOT.read_text())
    pos = []
    unreal = 0.0
    mv = 0.0
    for p in snap.get("positions", []):
        u = (p["last"] - p["avg_price"]) * p["qty"]
        v = p["last"] * p["qty"]
        unreal += u
        mv += v
        pos.append({**p, "unreal": round(u, 2), "mv": round(v, 2),
                    "pct": round((p["last"] / p["avg_price"] - 1) * 100, 2) if p["avg_price"] else 0,
                    "day_pct": round((p["last"] / p["prev_close"] - 1) * 100, 2) if p.get("prev_close") else 0})
    snap["positions"] = sorted(pos, key=lambda x: x["unreal"], reverse=True)
    snap["unreal_total"] = round(unreal, 2)
    snap["mv_total"] = round(mv, 2)
    return snap


# ---------------------------------------------------------------- HTML rendering
def cls(x: float) -> str:
    return "good" if x > 1e-9 else ("bad" if x < -1e-9 else "muted")


def stat_tile(label, value, sub, color=None):
    style = f' style="color:var(--{color})"' if color else ""
    return f"""<div class="panel"><h4>{label}</h4>
      <div class="stat"{style}>{value}</div><div class="stat-sub">{sub}</div></div>"""


def render(mode: str, d: dict, snap: dict | None, cost: dict) -> str:
    s = d["stats"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    net = round(s["realized"] - cost["total"], 2)

    # ---- headline tiles
    tiles = []
    if snap:
        pf = snap["portfolio"]
        tiles.append(stat_tile("Account value", fmt_usd(pf["total_value"], False),
                               f'equity {fmt_usd(pf["equity_value"],False)} · cash {fmt_usd(pf["cash"],False)}'))
        tiles.append(stat_tile("Open unrealized P&amp;L", fmt_usd(snap["unreal_total"]),
                               f'{len(snap["positions"])} open positions · mkt val {fmt_usd(snap["mv_total"],False)}',
                               cls(snap["unreal_total"])))
    tiles.append(stat_tile("Realized P&amp;L", fmt_usd(s["realized"]),
                           f'{s["n_trips"]} closed round-trips', cls(s["realized"])))
    # LLM token cost is THEORETICAL — this runs on a shared $100/mo plan (also used for work), so it's
    # not billed to the book. Show it as an efficiency signal, neutral-toned, with the hypothetical net.
    tiles.append(stat_tile("Token cost <span class=\"unit\">theoretical</span>", fmt_usd(cost["total"]),
                           f'not billed · shared $100/mo plan · net would be {fmt_usd(net)}'))
    tiles.append(stat_tile("Win rate", f'{s["winrate"]}<span class="unit">%</span>',
                           f'{s["n_wins"]} wins / {s["n_losses"]} losses'))
    pf_txt = "n/a" if s["pf"] is None else f'{s["pf"]:.2f}'
    tiles.append(stat_tile("Profit factor", pf_txt,
                           f'avg win {fmt_usd(s["avg_win"])} · avg loss {fmt_usd(s["avg_loss"])}'))
    if s["best"] and s["worst"]:
        tiles.append(stat_tile("Best / worst trip",
                               f'<span class="good">{fmt_usd(s["best"]["realized_usd"])}</span> / '
                               f'<span class="bad">{fmt_usd(s["worst"]["realized_usd"])}</span>',
                               f'{s["best"]["symbol"]} · {s["worst"]["symbol"]}'))

    # ---- theme concentration (open book) + data-integrity note
    theme_panel = integrity_note = ""
    if snap:
        by_theme = OrderedDict()
        for p in snap["positions"]:
            th = theme_of(p["symbol"])
            e = by_theme.setdefault(th, {"mv": 0.0, "unreal": 0.0, "syms": []})
            e["mv"] += p["mv"]; e["unreal"] += p["unreal"]; e["syms"].append(p["symbol"])
        tot_mv = sum(e["mv"] for e in by_theme.values()) or 1.0
        rows = "".join(
            f'<tr><td>{th}</td><td>{", ".join(e["syms"])}</td><td>{fmt_usd(e["mv"],False)}</td>'
            f'<td>{100*e["mv"]/tot_mv:.0f}%</td><td class="{cls(e["unreal"])}">{fmt_usd(e["unreal"])}</td></tr>'
            for th, e in sorted(by_theme.items(), key=lambda kv: -kv[1]["mv"]))
        theme_panel = (
            '<h3 class="sec">Theme concentration <span class="pill">open book</span></h3>'
            '<div class="panel"><table><thead><tr><th>Theme</th><th>Names</th><th>Mkt val</th>'
            '<th>% book</th><th>Unreal P&amp;L</th></tr></thead><tbody>' + rows + '</tbody></table>'
            '<p class="footnote">The held book is a momentum-theme bet, not diversified alpha. '
            'Watch the top row\'s % — single-theme drawdowns hit the whole book at once.</p></div>')
    drift = d.get("drift", [])
    if drift:
        dl = ", ".join(f'{x["symbol"]} Δ{x["delta"]:+g}' for x in drift)
        integrity_note = (f'<div class="callout" style="border-left-color:var(--warn)"><b>⚠ Ledger drift.</b>'
                          f'<p>{len(drift)} symbol(s) where broker fills don\'t reconstruct the open book '
                          f'({dl}) — older history beyond the fetched pages, or non-agentic fills.</p></div>')
    else:
        integrity_note = ('<div class="callout" style="border-left-color:var(--good)"><b>✓ Reconciled.</b>'
                          '<p>The broker-confirmed fills FIFO back to the exact open positions the broker '
                          'reports — realized P&amp;L below is settlement truth, not an estimate.</p></div>')

    # ---- chart data (JSON-embedded)
    curve_labels = [c["ts"][5:16] for c in d["curve"]]
    curve_vals = [c["cum"] for c in d["curve"]]
    curve_pts = [{"x": c["ts"][5:16], "sym": c["sym"], "pnl": c["pnl"]} for c in d["curve"]]
    daily_labels = [k for k, _ in d["daily"]]
    daily_vals = [v for _, v in d["daily"]]
    sym_labels = [p["sym"] for p in d["per_sym"]]
    sym_vals = [p["pnl"] for p in d["per_sym"]]

    # ---- exit-type table
    exit_tr = "".join(
        f'<tr><td>{r["label"]}</td><td>{r["n"]}</td>'
        f'<td class="{cls(r["pnl"])}">{fmt_usd(r["pnl"])}</td><td>{r["winpct"]}%</td></tr>'
        for r in d["exit_rows"])

    # ---- open positions table
    pos_tr = ""
    if snap:
        for p in snap["positions"]:
            pos_tr += (
                f'<tr><td>{p["symbol"]}</td><td>{p["qty"]:g}</td><td>${p["avg_price"]:.2f}</td>'
                f'<td>${p["last"]:.2f}</td><td>${p["mv"]:,.0f}</td>'
                f'<td class="{cls(p["unreal"])}">{fmt_usd(p["unreal"])}</td>'
                f'<td class="{cls(p["pct"])}">{p["pct"]:+.1f}%</td>'
                f'<td class="{cls(p["day_pct"])}">{p["day_pct"]:+.1f}%</td></tr>')

    # ---- round-trip table rows -> JSON for the sortable JS table
    trip_data = [{
        "ts": t["exit_ts"], "sym": t["symbol"], "qty": t["qty"],
        "entry": t["entry_price"], "exit": t["exit_price"],
        "pnl": t["realized_usd"], "pct": t["pnl_pct"], "hold": t["hold"],
        "exit_type": EXIT_LABEL.get(t["exit_type"], t["exit_type"] or "other"),
    } for t in d["trips"]]

    J = json.dumps
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>Agentic Book — Trades · Stocks · P&amp;L</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg:#0f1115; --panel:#181b22; --panel-2:#1f242d; --text:#e7ecf3; --muted:#9aa4b2;
    --accent:#4cc9f0; --good:#34d399; --warn:#fbbf24; --bad:#f87171; --line:#2a313c;
  }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:var(--bg); color:var(--text); margin:0; padding:24px; line-height:1.5; }}
  h1 {{ margin:0 0 4px; font-weight:700; letter-spacing:-0.02em; }}
  h1 small {{ font-weight:400; color:var(--muted); font-size:0.5em; margin-left:12px; }}
  h3.sec {{ margin:30px 0 12px; font-weight:600; font-size:1.05rem;
            padding-bottom:6px; border-bottom:2px solid var(--line); }}
  .sub {{ color:var(--muted); margin:0 0 20px; max-width:1100px; }}
  .sub code {{ background:var(--panel-2); padding:1px 6px; border-radius:5px; font-size:0.85em; }}
  .grid {{ display:grid; gap:16px; }}
  .grid-2 {{ grid-template-columns:repeat(auto-fit,minmax(420px,1fr)); }}
  .grid-3 {{ grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); }}
  .grid-tiles {{ grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); }}
  .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px; }}
  .panel h4 {{ margin:0 0 8px; font-size:0.78rem; color:var(--muted); text-transform:uppercase;
              letter-spacing:0.06em; font-weight:600; }}
  .stat {{ font-size:1.7rem; font-weight:700; letter-spacing:-0.02em; }}
  .stat .unit {{ font-size:0.5em; color:var(--muted); font-weight:500; margin-left:2px; }}
  .stat-sub {{ color:var(--muted); font-size:0.8rem; margin-top:3px; }}
  .good {{ color:var(--good); }} .bad {{ color:var(--bad); }} .muted {{ color:var(--muted); }}
  .chart-wrap {{ position:relative; height:300px; }}
  .chart-wrap.tall {{ height:420px; }}
  .callout {{ border-radius:12px; padding:12px 16px; margin:0 0 16px; border:1px solid var(--line);
             background:var(--panel); border-left:4px solid var(--accent); }}
  .callout p {{ margin:4px 0 0; color:var(--muted); font-size:0.85rem; }}
  .callout b {{ color:var(--text); }}
  table {{ width:100%; border-collapse:collapse; font-size:0.84rem; }}
  th, td {{ padding:6px 9px; text-align:right; border-bottom:1px solid var(--line); }}
  th:first-child, td:first-child {{ text-align:left; }}
  th {{ color:var(--muted); font-weight:600; text-transform:uppercase; font-size:0.66rem; letter-spacing:0.05em; }}
  tbody tr:hover {{ background:var(--panel-2); }}
  .footnote {{ color:var(--muted); font-size:0.78rem; margin-top:10px; }}
  .pill {{ display:inline-block; padding:2px 9px; border-radius:999px; font-size:0.72rem; font-weight:600;
           background:var(--panel-2); color:var(--muted); margin-left:8px; }}
  details.runit {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
                  margin:20px 0 12px; overflow:hidden; }}
  details.runit > summary {{ cursor:pointer; padding:14px 18px; font-weight:600; font-size:0.95rem;
                  list-style:none; user-select:none; display:flex; align-items:center; gap:10px; }}
  details.runit > summary::-webkit-details-marker {{ display:none; }}
  details.runit > summary::before {{ content:'▸'; color:var(--accent); transition:transform .15s; }}
  details.runit[open] > summary::before {{ transform:rotate(90deg); }}
  details.runit > summary:hover {{ background:var(--panel-2); }}
  details.runit .body {{ padding:4px 18px 18px; border-top:1px solid var(--line); }}
  pre.code {{ background:#0b0d11; border:1px solid var(--line); border-radius:8px; padding:14px 16px;
             overflow-x:auto; font-size:0.78rem; line-height:1.55; margin:10px 0 0;
             font-family:"SF Mono",Menlo,Consolas,monospace; color:#cdd6e4; }}
  pre.code .cmt {{ color:#6b7686; }}
  #tTable th[data-k] {{ cursor:pointer; user-select:none; white-space:nowrap; }}
  #tTable th[data-k]:hover {{ color:var(--text); }}
  #tTable th[aria-sort="ascending"]::after {{ content:' ▲'; font-size:0.8em; color:var(--accent); }}
  #tTable th[aria-sort="descending"]::after {{ content:' ▼'; font-size:0.8em; color:var(--accent); }}
</style>
</head>
<body>

<h1>Agentic Book <small>Trades · Stocks · P&amp;L &nbsp;|&nbsp; {mode.upper()} &nbsp;|&nbsp; generated {now}</small></h1>
<p class="sub">
  Live read of the autonomous Robinhood book (account <code>{snap['account'] if snap else '—'}</code>).
  Realized P&amp;L is FIFO over <b>broker-confirmed fills</b> (<code>get_equity_orders</code>) — settlement truth, reconciled
  to the broker's open positions. Unrealized and account value come from a broker snapshot at
  <code>{snap['captured_et'][:16].replace('T',' ') if snap else 'n/a'}</code> ET.
  Regenerate with <code>python3 scripts/dashboard.py</code>.
</p>
<div class="callout">
  <b>Read me first.</b>
  <p>Realized counts <b>closed</b> chunks only — open runners sit in unrealized until they exit. The
  <b>token cost</b> shown is <b>theoretical</b> — this runs on a shared $100/mo plan (also used for work), so it
  isn't billed to the book; it's an efficiency signal, not a real P&amp;L drag. Per-trade dollars are tiny by
  design (whole-share lots on a ~$3k cash account); read the <b>shape</b>, not the magnitude. Source of realized
  truth: broker fills, not our event log.</p>
</div>

{integrity_note}

<h3 class="sec">Headline</h3>
<div class="grid grid-tiles">{''.join(tiles)}</div>

{theme_panel}

<h3 class="sec">P&amp;L over time</h3>
<div class="grid grid-2">
  <div class="panel">
    <h4>Cumulative realized P&amp;L <span class="pill">{s['n_trips']} trips</span></h4>
    <div class="chart-wrap"><canvas id="cCurve"></canvas></div>
    <p class="footnote">Running sum of realized $ across closed round-trips, in exit order.</p>
  </div>
  <div class="panel">
    <h4>Daily realized P&amp;L</h4>
    <div class="chart-wrap"><canvas id="cDaily"></canvas></div>
    <p class="footnote">One bar per ET trading day. Green = up day, red = down day (realized only).</p>
  </div>
</div>

<h3 class="sec">By symbol &amp; exit</h3>
<div class="grid grid-2">
  <div class="panel">
    <h4>Net realized by symbol</h4>
    <div class="chart-wrap tall"><canvas id="cSym"></canvas></div>
    <p class="footnote">Every name with a closed chunk, ranked worst&rarr;best.</p>
  </div>
  <div class="panel">
    <h4>How positions closed</h4>
    <table><thead><tr><th>Exit type</th><th>n</th><th>Realized</th><th>Win%</th></tr></thead>
      <tbody>{exit_tr}</tbody></table>
    <p class="footnote">Exit type parsed from the sell reason the engine wrote at close.</p>
  </div>
</div>

{'<h3 class="sec">Open positions</h3><div class="panel"><table><thead><tr><th>Symbol</th><th>Qty</th><th>Avg cost</th><th>Last</th><th>Mkt val</th><th>Unreal P&amp;L</th><th>vs cost</th><th>day</th></tr></thead><tbody>' + pos_tr + '</tbody></table><p class="footnote">Unrealized from the broker snapshot; sorted by unrealized P&amp;L.</p></div>' if snap else ''}

<h3 class="sec">Round-trips <span class="pill">click a header to sort</span></h3>
<div class="panel">
  <table id="tTable"><thead><tr>
    <th data-k="ts">Exit (ET)</th><th data-k="sym">Symbol</th><th data-k="qty">Qty</th>
    <th data-k="entry">Entry</th><th data-k="exit">Exit</th><th data-k="pnl" aria-sort="descending">P&amp;L</th>
    <th data-k="pct">P&amp;L%</th><th data-k="hold">Hold</th><th data-k="exit_type">Exit type</th>
  </tr></thead><tbody id="tBody"></tbody></table>
</div>

<details class="runit">
  <summary>Run it yourself</summary>
  <div class="body">
    <p>This page is generated from the trade ledger and a broker snapshot — nothing here is hand-typed.</p>
    <pre class="code"><span class="cmt"># refresh the live snapshot (portfolio + positions + quotes via the MCP), then rebuild:</span>
python3 scripts/dashboard.py            <span class="cmt"># -> data/dashboard.html (live book)</span>
python3 scripts/dashboard.py --mode paper

<span class="cmt"># the underlying CLIs this mirrors:</span>
python3 scripts/trade_ledger.py --round-trips   <span class="cmt"># FIFO entry->exit pairing</span>
python3 scripts/pnl_report.py --by-day          <span class="cmt"># realized P&L + exit-type breakdown</span></pre>
  </div>
</details>

<script>
const C = {{good:'#34d399', bad:'#f87171', accent:'#4cc9f0', muted:'#9aa4b2', line:'#2a313c', grid:'rgba(255,255,255,0.05)'}};
Chart.defaults.color = C.muted;
Chart.defaults.font.family = "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif";
const sgn = v => (v>=0?'+':'-')+'$'+Math.abs(v).toFixed(2);

const curveVals = {J(curve_vals)}, curveLabels = {J(curve_labels)}, curvePts = {J(curve_pts)};
new Chart(document.getElementById('cCurve'), {{
  type:'line',
  data:{{labels:curveLabels, datasets:[{{
    data:curveVals, borderColor:C.accent, backgroundColor:'rgba(76,201,240,0.10)',
    fill:true, tension:0.25, pointRadius:2, pointHoverRadius:5, borderWidth:2,
  }}]}},
  options:{{maintainAspectRatio:false, plugins:{{legend:{{display:false}}, tooltip:{{callbacks:{{
    title:(t)=>curvePts[t[0].dataIndex].sym+'  '+curveLabels[t[0].dataIndex],
    label:(t)=>'cum '+sgn(t.parsed.y)+'   (this: '+sgn(curvePts[t.dataIndex].pnl)+')'}}}}}},
    scales:{{x:{{grid:{{color:C.grid}}, ticks:{{maxTicksLimit:8}}}}, y:{{grid:{{color:C.grid}}, ticks:{{callback:v=>'$'+v}}}}}}}}
}});

const dVals = {J(daily_vals)};
new Chart(document.getElementById('cDaily'), {{
  type:'bar',
  data:{{labels:{J(daily_labels)}, datasets:[{{
    data:dVals, backgroundColor:dVals.map(v=>v>=0?C.good:C.bad), borderRadius:3}}]}},
  options:{{maintainAspectRatio:false, plugins:{{legend:{{display:false}}, tooltip:{{callbacks:{{
    label:t=>sgn(t.parsed.y)}}}}}},
    scales:{{x:{{grid:{{display:false}}}}, y:{{grid:{{color:C.grid}}, ticks:{{callback:v=>'$'+v}}}}}}}}
}});

const symVals = {J(sym_vals)};
new Chart(document.getElementById('cSym'), {{
  type:'bar',
  data:{{labels:{J(sym_labels)}, datasets:[{{
    data:symVals, backgroundColor:symVals.map(v=>v>=0?C.good:C.bad), borderRadius:3}}]}},
  options:{{indexAxis:'y', maintainAspectRatio:false, plugins:{{legend:{{display:false}}, tooltip:{{callbacks:{{
    label:t=>sgn(t.parsed.x)}}}}}},
    scales:{{x:{{grid:{{color:C.grid}}, ticks:{{callback:v=>'$'+v}}}}, y:{{grid:{{display:false}}, ticks:{{font:{{size:10}}}}}}}}}}
}});

// ---- sortable round-trip table
const TRIPS = {J(trip_data)};
const cls = v => v>1e-9?'good':(v<-1e-9?'bad':'muted');
const usd = v => (v<0?'-':'')+'$'+Math.abs(v).toFixed(2);
let sortK='pnl', sortDir=-1;
function paint(){{
  const data=[...TRIPS].sort((a,b)=>{{
    let x=a[sortK], y=b[sortK];
    if(typeof x==='number') return (x-y)*sortDir;
    return String(x).localeCompare(String(y))*sortDir;
  }});
  document.getElementById('tBody').innerHTML = data.map(t=>
    `<tr><td>${{t.ts.slice(5)}}</td><td>${{t.sym}}</td><td>${{(+t.qty).toFixed(t.qty<1?4:0)}}</td>`+
    `<td>$${{(+t.entry).toFixed(2)}}</td><td>$${{(+t.exit).toFixed(2)}}</td>`+
    `<td class="${{cls(t.pnl)}}">${{usd(t.pnl)}}</td>`+
    `<td class="${{cls(t.pct)}}">${{t.pct>=0?'+':''}}${{t.pct.toFixed(1)}}%</td>`+
    `<td>${{t.hold}}</td><td class="muted">${{t.exit_type}}</td></tr>`).join('');
  document.querySelectorAll('#tTable th[data-k]').forEach(th=>{{
    th.setAttribute('aria-sort', th.dataset.k===sortK?(sortDir<0?'descending':'ascending'):'none');
  }});
}}
document.querySelectorAll('#tTable th[data-k]').forEach(th=>th.addEventListener('click',()=>{{
  const k=th.dataset.k;
  if(k===sortK) sortDir*=-1; else {{sortK=k; sortDir=(k==='sym'||k==='exit_type'||k==='ts')?1:-1;}}
  paint();
}}));
paint();
</script>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="live", help="live | paper | all")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    if args.mode == "live":
        d = truth_to_display(load_truth())   # broker-confirmed fills (settlement truth)
        snap = load_snapshot()
    else:
        d = compute(args.mode)               # paper FIFO over our log (paper sells carry a price)
        snap = None
    cost = total_cost(args.mode)
    html = render(args.mode, d, snap, cost)
    Path(args.out).write_text(html)
    net = d["stats"]["realized"] - cost["total"]
    print(f"wrote {args.out}  ({len(html):,} bytes)")
    print(f"  {d['stats']['n_trips']} round-trips · realized {fmt_usd(d['stats']['realized'])} · "
          f"win-rate {d['stats']['winrate']}% · net-of-cost {fmt_usd(net)} (tokens {fmt_usd(cost['total'])})")
    if snap:
        print(f"  {len(snap['positions'])} open · unrealized {fmt_usd(snap['unreal_total'])} · "
              f"acct {fmt_usd(snap['portfolio']['total_value'],False)}")


if __name__ == "__main__":
    main()
