#!/usr/bin/env python3
"""
decide.py — two-stage decision orchestrator for one tick. Produces the final action list; the
deterministic executor (apply_decision.py) still re-checks caps and logs.

  Stage 1  (DETERMINISTIC, no LLM — computed in tick_context.py): screen the packet -> {exits, entry_candidates}
  Stage 2  (only if there are entry_candidates AND allow_entries): for each shortlisted name,
           dd_probe.py gathers deep quant DD, then DD_MODEL (default Sonnet) + web news/catalyst
           search decides commit/reject + size. Default is REJECT.

Exits execute immediately (mechanical risk mgmt). Entries only after Stage-2 commits. Output:
data/tick/decision_latest.json = {actions, rationale, screen, dd}. The LLMs only judge; all data
gathering is scripts.
"""
from __future__ import annotations

import concurrent.futures
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")            # cache freshness is measured in ET calendar days

import stock_memory as memory                # sibling: long-term per-symbol eval memory + exclusions
import catalyst_log                          # sibling: forward filter-lift ledger (gap event -> verdict)
from apply_decision import extract_decision  # sibling: robust JSON extraction

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
TICK = REPO / "data" / "tick"
DD_CACHE = REPO / "data" / "dd_cache.json"
MANAGE_CACHE = REPO / "data" / "manage_cache.json"   # Tier-2: last-managed ts + verdict per holding
DD_JOBS = REPO / "data" / "dd_jobs"                  # async (DD_ASYNC) mode: one <SYM>.json job/result file
PYEXE = sys.executable or "python3"


def prompt_text(fname: str) -> str:
    """Read a prompt template, substituting {MODE} with the real execution mode (PAPER/LIVE).
    TRADING_MODE is forced by the entry script (run_paper_tick.sh / run_live_tick.sh), so the
    agent always knows whether its commits move real money — the header used to hardcode one mode."""
    mode = (os.environ.get("TRADING_MODE") or "paper").upper()
    return (SCRIPTS / fname).read_text().replace("{MODE}", mode)


def load_cache() -> dict:
    try:
        return json.loads(DD_CACHE.read_text())
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict) -> None:
    DD_CACHE.parent.mkdir(parents=True, exist_ok=True)
    DD_CACHE.write_text(json.dumps(cache, indent=2))


def load_manage_cache() -> dict:
    try:
        return json.loads(MANAGE_CACHE.read_text())
    except (OSError, ValueError):
        return {}


def save_manage_cache(cache: dict) -> None:
    MANAGE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    MANAGE_CACHE.write_text(json.dumps(cache, indent=2))


# --------------------------------------------------------------------------- async DD (DD_ASYNC)
# In async mode the entry DDs don't BLOCK the tick: cache misses are dispatched to detached
# dd_worker.py processes that write data/dd_jobs/<SYM>.json, and a LATER tick ingests the finished
# verdicts into the cache and acts on them. decide.py stays the SOLE dd_cache writer (workers only
# touch their own job file), so no cross-process cache lock is needed. Knobs:
#   DD_ASYNC=1                  turn it on (default 0 = the original synchronous, blocking path)
#   DD_ASYNC_MAX_INFLIGHT=6     ceiling on concurrent background workers (bounds CPU/RAM + spend)
#   DD_ASYNC_RUNNING_TIMEOUT_S  a 'running' marker older than this => worker died, reap + re-dispatch
def _async_on() -> bool:
    return os.environ.get("DD_ASYNC", "0").strip().lower() in ("1", "true", "yes")


def _running_timeout() -> int:
    return int(os.environ.get("DD_ASYNC_RUNNING_TIMEOUT_S", "600"))


def job_in_flight(sym: str, now: float) -> bool:
    """True if a background DD for sym is dispatched and not yet finished (fresh 'running' marker)."""
    try:
        job = json.loads((DD_JOBS / f"{sym.upper()}.json").read_text())
    except (OSError, ValueError):
        return False
    return job.get("status") == "running" and (now - job.get("ts", 0)) <= _running_timeout()


def count_in_flight(now: float) -> int:
    if not DD_JOBS.exists():
        return 0
    n = 0
    for f in DD_JOBS.glob("*.json"):
        try:
            job = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if job.get("status") == "running" and (now - job.get("ts", 0)) <= _running_timeout():
            n += 1
    return n


def ingest_dd_jobs(cache: dict, today_et: str, now: float) -> set[str]:
    """Fold FINISHED background verdicts into the cache (commit/reject only — an error worker is
    dropped so it retries) and reap dead 'running' markers. Returns the SET of symbols whose verdicts
    were folded in THIS tick — the entry pass treats those as freshly-produced (ts==now) and is allowed
    to BUY on them, while an older cached commit is re-DD'd before any buy (see the cache-read loop)."""
    if not DD_JOBS.exists():
        return set()
    ingested: set[str] = set()
    for f in DD_JOBS.glob("*.json"):
        try:
            job = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        sym = (job.get("symbol") or f.stem).upper()
        status = job.get("status")
        if status == "done":
            res = job.get("result") or {}
            # Fold the detached worker's token spend into THIS tick's ledger so the end-of-tick TOKENS
            # line accounts for async DD cost. Attribute to the ingest tick (the dispatching tick paid
            # nothing — it only spawned). Relabel "async:" so the per-call breakdown stays traceable.
            # Done for every finished job (commit/reject/error) so the cost picture is complete.
            for _call in ((job.get("usage") or {}).get("calls") or []):
                _rec = dict(_call)
                _rec["label"] = f"async:{_rec.get('label') or ('dd:' + sym)}"
                with _USAGE_LOCK:
                    _USAGE_LEDGER.append(_rec)
            if res.get("decision") in ("commit", "reject"):
                cache[sym] = {"ts": now, "day": today_et,
                              "ref_price": job.get("ref_price"), "ref_range_pos": job.get("ref_range_pos"),
                              "result": {k: v for k, v in res.items() if k != "dd_elapsed_s"}}
                memory.record(sym, decision=res["decision"], conviction=res.get("conviction"),
                              reason=res.get("reason", ""), catalysts=res.get("catalysts"),
                              risks=res.get("risks"), next_earnings_date=res.get("next_earnings_date"),
                              never_buy=bool(res.get("never_buy")), never_buy_reason=res.get("never_buy_reason"))
                ingested.add(sym)
            try:
                f.unlink()  # consumed (or a dropped error) — clear it either way
            except OSError:
                pass
        elif status == "running" and (now - job.get("ts", 0)) > _running_timeout():
            try:
                f.unlink()  # worker died -> reap so the symbol can be re-dispatched
            except OSError:
                pass
    return ingested


def dispatch_dd(sym: str, c: dict, now: float) -> bool:
    """Fire-and-forget a detached dd_worker for one symbol, writing the 'running' marker first so a
    later tick won't double-dispatch. Returns True if spawned."""
    sym = sym.upper()
    try:
        DD_JOBS.mkdir(parents=True, exist_ok=True)
        tmp = (DD_JOBS / f"{sym}.json").with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"symbol": sym, "status": "running", "ts": now}))
        os.replace(tmp, DD_JOBS / f"{sym}.json")
        args = [PYEXE, str(SCRIPTS / "dd_worker.py"), sym, "--reason", str(c.get("reason", "async DD"))]
        for flag, key in (("--last", "last"), ("--range-pos", "range_pos"), ("--intraday", "intraday_pct")):
            if c.get(key) is not None:
                args += [flag, str(c[key])]
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)  # detach so it outlives this tick
        return True
    except OSError:
        return False


def claude_bin() -> str:
    return (os.environ.get("AGENTIC_CLAUDE") or shutil.which("claude")
            or str(Path.home() / ".local/bin/claude"))


# --- Per-tick token accounting ----------------------------------------------------------------
# Every model call in a tick funnels through run_claude (entry DDs, manage DDs, and the live
# MCP relay), and those calls run in a ThreadPoolExecutor. Each call appends its usage to this
# ledger under a lock, so usage_summary() at end-of-tick is an exact per-tick token/cost total.
_USAGE_LOCK = threading.Lock()
_USAGE_LEDGER: list[dict] = []


def reset_usage() -> None:
    with _USAGE_LOCK:
        _USAGE_LEDGER.clear()


def _record_usage(label: str, model: str, usage: dict | None, cost_usd: float | None,
                  elapsed_s: float, error: str | None = None) -> None:
    """Thread-safe append of one headless-Claude call's token + cost figures."""
    rec = {"label": label, "model": model, "elapsed_s": round(elapsed_s, 1)}
    if error:
        rec["error"] = error
    if isinstance(usage, dict):
        for k in ("input_tokens", "output_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens"):
            rec[k] = usage.get(k)
    if cost_usd is not None:
        rec["cost_usd"] = cost_usd
    with _USAGE_LOCK:
        _USAGE_LEDGER.append(rec)


def usage_summary() -> dict:
    """Roll the tick's headless-Claude calls into a token/cost total + per-call breakdown."""
    with _USAGE_LOCK:
        calls = list(_USAGE_LEDGER)

    def _sum(key: str) -> int:
        return sum(int(c.get(key) or 0) for c in calls)

    inp, out = _sum("input_tokens"), _sum("output_tokens")
    cc, cr = _sum("cache_creation_input_tokens"), _sum("cache_read_input_tokens")
    return {
        "n_calls": len(calls),
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
        "total_tokens": inp + out + cc + cr,    # all billed tokens (cached input is billed too)
        "cost_usd": round(sum(float(c.get("cost_usd") or 0.0) for c in calls), 4),
        "calls": calls,
    }


# --- live progress narration --------------------------------------------------------------------
# Every LLM and every broker-relay call funnels through run_claude() below, and each is a 10-60s
# headless `claude` subprocess whose output is CAPTURED, not streamed. Without narration a tick is
# minutes of dead silence. _progress() prints a timestamped, flushed line to stdout (the run
# scripts tee stdout to the terminal + run log) bracketing each call — what's running, how long it
# took, what it cost — so the operator can watch a tick unfold live instead of staring at a frozen
# terminal. Set TICK_PROGRESS=0 to silence it (e.g. in tests).
def _progress_on() -> bool:
    return os.environ.get("TICK_PROGRESS", "1").strip().lower() not in ("0", "false", "no", "")


def _progress(msg: str) -> None:
    if not _progress_on():
        return
    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)
    except Exception:  # noqa: BLE001 — narration must never break a tick
        pass


def _short_model(model: str) -> str:
    m = (model or "").lower()
    for tag in ("opus", "sonnet", "haiku"):
        if tag in m:
            return tag
    return ((model or "?").split("-") or ["?"])[0] or "?"


def _fmt_tok(n: int | None) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _fmt_usd(cost) -> str:
    try:
        return f"${float(cost):.4f}" if cost is not None else "$?"
    except (TypeError, ValueError):
        return "$?"


def _usage_total(usage: dict | None) -> int:
    if not isinstance(usage, dict):
        return 0
    return sum(int(usage.get(k) or 0) for k in
              ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def _heartbeat_every() -> int:
    try:
        return max(0, int(os.environ.get("TICK_PROGRESS_HEARTBEAT_S", "20")))
    except ValueError:
        return 20


def _start_heartbeat(label: str, t0: float) -> threading.Event:
    """Tick out a '...still running (Ns)' line every N seconds while a long call is in flight, so a
    60-180s broker snapshot or DD isn't a silent void. Returns a stop Event the caller .set()s when
    the call finishes (a no-op if narration/heartbeat is disabled)."""
    stop = threading.Event()
    every = _heartbeat_every()
    if not _progress_on() or every <= 0:
        return stop

    def _beat() -> None:
        while not stop.wait(every):
            _progress(f"    · {label} still running ({int(time.time() - t0)}s)")

    threading.Thread(target=_beat, daemon=True).start()
    return stop


def _parse_claude_json(stdout: str) -> tuple[str | None, dict | None, float | None]:
    """Pull (result_text, usage, total_cost_usd) out of `claude --output-format json`. Falls back
    to treating stdout as raw text with no usage if it isn't the expected JSON envelope."""
    try:
        obj = json.loads(stdout)
    except (ValueError, TypeError):
        return (stdout or None), None, None
    if not isinstance(obj, dict):
        return (stdout or None), None, None
    return obj.get("result"), obj.get("usage"), obj.get("total_cost_usd")


def run_claude(prompt: str, model: str, tools: list | None = None, mcp: bool = False,
               timeout: int = 360, label: str = "claude") -> str | None:
    """Headless Claude. --strict-mcp-config => only the servers we pass (none, or the RH MCP).

    Returns the model's result text on success, or None on timeout / non-zero exit / empty output —
    so the caller can tell a model FAILURE (retry next tick, don't cache) apart from a real 'reject'
    verdict. Uses --output-format json so each call's token usage + cost are captured into the tick
    ledger (see usage_summary()); `label` attributes the spend (e.g. "dd:AAPL", "manage:MSFT").
    """
    cmd = [claude_bin(), "-p", prompt, "--model", model, "--output-format", "json",
           "--strict-mcp-config"]
    if mcp:
        cmd += ["--mcp-config", str(REPO / ".mcp.json")]
    if tools:
        cmd += ["--allowedTools", *tools, "--dangerously-skip-permissions"]
    _progress(f"  → {label} ({_short_model(model)}) running…")
    t0 = time.time()
    stop_hb = _start_heartbeat(label, t0)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        el = time.time() - t0
        sys.stderr.write(f"[decide] claude invocation failed: {e}\n")
        _record_usage(label, model, None, None, el, error=str(e)[:120])
        _progress(f"  ✗ {label} failed after {el:.0f}s ({type(e).__name__})")
        return None
    finally:
        stop_hb.set()
    if r.returncode != 0:
        el = time.time() - t0
        sys.stderr.write(f"[decide] claude exit {r.returncode}: {(r.stderr or '')[:300]}\n")
        _record_usage(label, model, None, None, el, error=f"exit_{r.returncode}")
        _progress(f"  ✗ {label} exit {r.returncode} after {el:.0f}s")
        return None
    text, usage, cost = _parse_claude_json(r.stdout)
    el = time.time() - t0
    _record_usage(label, model, usage, cost, el)
    _progress(f"  ✓ {label} {el:.0f}s · {_fmt_tok(_usage_total(usage))} tok · {_fmt_usd(cost)}")
    return text if (text and text.strip()) else None


# Stage-2 DD agent toolset: quant is script-gathered; the agent adds live quote + news research.
DD_TOOLS = ["WebSearch", "WebFetch", "mcp__robinhood-trading__get_equity_quotes"]


def fmt_candidate(c: dict) -> str:
    """One screened entry candidate -> compact signal string for the log."""
    return f"{c.get('symbol', '?')}(+{c.get('intraday_pct')}% intraday)"


def fmt_dd_line(d: dict) -> str:
    """One Stage-2 DD verdict -> a human line explaining WHY it committed / rejected / errored.

    Shows the verdict, whether it came from a fresh model call or the TTL cache (and how old),
    the size on a commit, and the model's own reason — so a reject is never a silent count.
    """
    sym = d.get("symbol", "?")
    dec = (d.get("decision") or "?").upper()
    if d.get("cached"):
        src = f"cached {d.get('cached_age_min', '?')}m"   # reused verdict — explains a fast tick
    elif d.get("dd_elapsed_s") is not None:
        src = f"fresh {d['dd_elapsed_s']}s"                # live Sonnet+web call — explains a slow tick
    else:
        src = "fresh"
    reason = (d.get("reason") or d.get("error") or "(no reason given)").strip().replace("\n", " ")
    size = f" ${d.get('dollar_amount')} {d.get('conviction')}" if dec == "COMMIT" else ""
    return f"  DD {sym}: {dec} [{src}]{size} — {reason[:300]}"


def run_dd(c: dict, regime: dict, caps: dict, portfolio: dict, dd_model: str) -> dict:
    """Stage-2 research agent for one symbol: deep quant probe + multi-tool news/catalyst commit.

    `decision` is one of commit / reject / error. 'error' (model timeout / non-zero exit /
    unparseable output) is NOT a verdict — callers must not buy on it and must not cache it.
    """
    sym = str(c.get("symbol", "")).upper().strip()
    try:
        subprocess.run([PYEXE, str(SCRIPTS / "dd_probe.py"), sym],
                       capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as e:
        sys.stderr.write(f"[decide] dd_probe {sym} failed ({e}); using any existing probe file\n")
    dd_file = TICK / f"dd_{sym}.json"
    try:
        dd = json.loads(dd_file.read_text()) if dd_file.exists() else {"symbol": sym, "error": "no_dd"}
    except (OSError, ValueError):
        dd = {"symbol": sym, "error": "dd_unreadable"}

    # Python pre-filter: if the probe already triggers a hard R1 disqualifier (can't price /
    # can't exit), auto-reject here and skip the ~85s Sonnet call — the model would reject anyway.
    pre = _r1_reject(sym, dd)
    if pre is not None:
        sys.stderr.write(f"[decide] {sym}: R1 pre-filter → reject ({pre['reason']})\n")
        return pre

    # Headroom = how much we could ACTUALLY add now (cap, remaining exposure, and cash), so the
    # model sizes within what the deterministic gate will accept instead of proposing a number
    # that silently rejects.
    headroom = max(0.0, min(caps["MAX_POSITION_USD"],
                            caps["MAX_TOTAL_EXPOSURE_USD"] - portfolio.get("exposure", 0.0),
                            portfolio.get("cash", 0.0)))
    dd_input = json.dumps({
        "symbol": sym,
        "screen_reason": c.get("reason", ""),
        "screen_signal": {"intraday_pct": c.get("intraday_pct"),  # move from today's open
                          "range_pos": c.get("range_pos"),       # near 1.0 = at the day's high
                          "last": c.get("last")},                # the screen had no signal gate — you pick
        "regime": regime,
        "sizing": {"MAX_POSITION_USD": caps["MAX_POSITION_USD"],
                   "MIN_POSITION_USD": caps.get("MIN_POSITION_USD", 0.0),
                   "available_headroom_usd": round(headroom, 2),
                   "available_cash": round(portfolio.get("cash", 0.0), 2),
                   "stop_loss_pct": caps["STOP_LOSS_PCT"],
                   "take_profit_pct": caps["TAKE_PROFIT_PCT"],
                   "max_per_trade_loss_usd": caps.get("MAX_PER_TRADE_LOSS_USD")},
        "portfolio": {"open_positions": portfolio.get("open_positions", 0),
                      "max_open_positions": caps["MAX_OPEN_POSITIONS"],
                      "held_symbols": portfolio.get("held", [])},
        # Prime with our OWN past verdicts on this name (None if never evaluated): if the same
        # disqualifier still holds, the model rejects fast; if the picture changed, it says so.
        "prior_evaluation": memory.get_note(sym),
        "dd": dd,
    })
    # Per-DD wall-clock cap. Kept tight (default 240s) so that — even when several DDs run in
    # parallel — one slow web-research call can't push the tick past the ~5-min launchd cadence.
    dd_timeout = int(os.environ.get("DD_CLAUDE_TIMEOUT_S", "240"))
    out = run_claude(prompt_text("dd_prompt.txt") + dd_input,
                     dd_model, tools=DD_TOOLS, mcp=True, timeout=dd_timeout, label=f"dd:{sym}")
    if out is None:
        return {"symbol": sym, "decision": "error", "error": "dd_model_failed", "conviction": None,
                "dollar_amount": None, "reason": "", "catalysts": [], "risks": []}
    commit = extract_decision(out)
    if commit.get("_parse_error"):
        return {"symbol": sym, "decision": "error", "error": "dd_parse_error", "conviction": None,
                "dollar_amount": None, "reason": "model output unparseable", "catalysts": [],
                "risks": [], "raw_excerpt": out.strip()[:300]}
    decision = (commit.get("decision") or "reject").lower()
    dollar = commit.get("dollar_amount")
    # Commit invariant: a commit MUST carry a positive, finite dollar_amount — otherwise it would
    # silently evaporate at the buy step. Downgrade such a "commit" to a logged reject.
    if decision == "commit":
        try:
            d = float(dollar)
            if not math.isfinite(d) or d <= 0:
                raise ValueError
        except (TypeError, ValueError):
            decision, dollar = "reject", None
            commit["reason"] = f"commit returned without a valid size; {commit.get('reason', '')}".strip()
    return {"symbol": sym, "decision": decision, "conviction": commit.get("conviction"),
            "dollar_amount": dollar, "reason": commit.get("reason", ""),
            "catalyst_type": commit.get("thesis_type") or commit.get("catalyst_type"),  # the agent's thesis tag
            "hold_intent": commit.get("hold_intent"),            # scalp | swing | runner — agent's horizon call
            "entry_trigger": commit.get("entry_trigger"),        # optional {price,direction} -> sentinel-armed entry
            "catalysts": commit.get("catalysts", []), "risks": commit.get("risks", []),
            "next_earnings_date": commit.get("next_earnings_date"),
            "pead_qualified": dd.get("pead_qualified"),          # measured gap+vol signal met (label gate, P3)
            "washout_reversal": dd.get("washout_reversal"),      # gap-down recovery shape (label-only)
            "iv30": dd.get("iv30"),                              # ENTRY-time vol (A12): IV crushes post-
            "rvol20": dd.get("realized_vol_20d_annual_pct"),     # catalyst, so it can't be backfilled later
            "never_buy": bool(commit.get("never_buy")),          # structural disqualifier -> exclude
            "never_buy_reason": commit.get("never_buy_reason")}


def run_dd_batch(fresh_jobs: list[tuple[str, dict]], regime: dict, caps: dict,
                 portfolio: dict, dd_model: str) -> dict[str, dict]:
    """Evaluate all cache-miss candidates in ONE claude subprocess, amortising the startup cost
    (system prompt + tool schemas) once instead of N times.

    Tradeoff vs the parallel approach: Claude processes symbols sequentially inside one conversation,
    so wall-clock is ~sum(per-symbol) rather than max(per-symbol). Worth it when N is small (2-3)
    and token waste dominates over latency.

    Returns {sym: verdict_dict} with the same shape as run_dd().
    """
    if not fresh_jobs:
        return {}

    # Run all dd_probe scripts in parallel first (same as the single-symbol path).
    def probe_one(sym_c: tuple[str, dict]) -> tuple[str, dict]:
        sym, _ = sym_c
        try:
            subprocess.run([PYEXE, str(SCRIPTS / "dd_probe.py"), sym],
                           capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as e:
            sys.stderr.write(f"[decide] dd_probe {sym} failed ({e}); using any existing probe file\n")
        dd_file = TICK / f"dd_{sym}.json"
        try:
            dd = json.loads(dd_file.read_text()) if dd_file.exists() else {"symbol": sym, "error": "no_dd"}
        except (OSError, ValueError):
            dd = {"symbol": sym, "error": "dd_unreadable"}
        return sym, dd

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(fresh_jobs), 6)) as ex:
        probed: dict[str, dict] = dict(ex.map(probe_one, fresh_jobs))

    # Python pre-filter: auto-reject any symbol the probe already hard-disqualifies (R1).
    # Splits fresh_jobs into auto_rejected (no model call) and model_jobs (need the batch call).
    auto_rejected: dict[str, dict] = {}
    model_jobs: list[tuple[str, dict]] = []
    for sym, c in fresh_jobs:
        pre = _r1_reject(sym, probed.get(sym, {"symbol": sym, "error": "no_dd"}))
        if pre is not None:
            sys.stderr.write(f"[decide] {sym}: R1 pre-filter → reject ({pre['reason']})\n")
            auto_rejected[sym] = pre
        else:
            model_jobs.append((sym, c))

    if not model_jobs:
        return auto_rejected

    # Build batch input array (only symbols that passed the pre-filter).
    batch_items = []
    for sym, c in model_jobs:
        headroom = max(0.0, min(caps["MAX_POSITION_USD"],
                                caps["MAX_TOTAL_EXPOSURE_USD"] - portfolio.get("exposure", 0.0),
                                portfolio.get("cash", 0.0)))
        batch_items.append({
            "symbol": sym,
            "screen_reason": c.get("reason", ""),
            "screen_signal": {"intraday_pct": c.get("intraday_pct"),
                              "range_pos": c.get("range_pos"),
                              "last": c.get("last")},
            "regime": regime,
            "sizing": {"MAX_POSITION_USD": caps["MAX_POSITION_USD"],
                       "MIN_POSITION_USD": caps.get("MIN_POSITION_USD", 0.0),
                       "available_headroom_usd": round(headroom, 2),
                       "available_cash": round(portfolio.get("cash", 0.0), 2),
                       "stop_loss_pct": caps["STOP_LOSS_PCT"],
                       "take_profit_pct": caps["TAKE_PROFIT_PCT"],
                       "max_per_trade_loss_usd": caps.get("MAX_PER_TRADE_LOSS_USD")},
            "portfolio": {"open_positions": portfolio.get("open_positions", 0),
                          "max_open_positions": caps["MAX_OPEN_POSITIONS"],
                          "held_symbols": portfolio.get("held", [])},
            "prior_evaluation": memory.get_note(sym),
            "dd": probed.get(sym, {"symbol": sym, "error": "no_dd"}),
        })

    dd_timeout = int(os.environ.get("DD_CLAUDE_TIMEOUT_S", "240")) * len(model_jobs)
    out = run_claude(prompt_text("dd_batch_prompt.txt") + json.dumps(batch_items),
                     dd_model, tools=DD_TOOLS, mcp=True, timeout=dd_timeout, label="dd:batch")

    # Error path: model failed — return error verdict for every model-evaluated symbol.
    if out is None:
        return {**auto_rejected, **{sym: {"symbol": sym, "decision": "error", "error": "dd_model_failed",
                                          "conviction": None, "dollar_amount": None, "reason": "",
                                          "catalysts": [], "risks": []}
                                    for sym, _ in model_jobs}}

    # Parse the JSON array response. Try to recover a partial array if the model wrapped it.
    verdicts: list[dict] = []
    try:
        parsed = json.loads(out)
        verdicts = parsed if isinstance(parsed, list) else [parsed]
    except (ValueError, TypeError):
        import re as _re
        m = _re.search(r'\[.*\]', out, _re.DOTALL)
        if m:
            try:
                verdicts = json.loads(m.group())
            except (ValueError, TypeError):
                verdicts = []

    verdict_map: dict[str, dict] = {}
    for v in verdicts:
        if isinstance(v, dict):
            s = (v.get("symbol") or "").upper()
            if s:
                verdict_map[s] = v

    results: dict[str, dict] = {}
    for sym, _ in model_jobs:
        commit = verdict_map.get(sym)
        if commit is None:
            results[sym] = {"symbol": sym, "decision": "error", "error": "missing_from_batch",
                            "conviction": None, "dollar_amount": None, "reason": "", "catalysts": [],
                            "risks": []}
            continue
        if commit.get("_parse_error"):
            results[sym] = {"symbol": sym, "decision": "error", "error": "dd_parse_error",
                            "conviction": None, "dollar_amount": None,
                            "reason": "model output unparseable", "catalysts": [], "risks": []}
            continue
        decision = (commit.get("decision") or "reject").lower()
        dollar = commit.get("dollar_amount")
        if decision == "commit":
            try:
                d = float(dollar)
                if not math.isfinite(d) or d <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                decision, dollar = "reject", None
                commit["reason"] = f"commit returned without a valid size; {commit.get('reason', '')}".strip()
        results[sym] = {
            "symbol": sym, "decision": decision, "conviction": commit.get("conviction"),
            "dollar_amount": dollar, "reason": commit.get("reason", ""),
            "catalyst_type": commit.get("thesis_type") or commit.get("catalyst_type"),
            "hold_intent": commit.get("hold_intent"),
            "entry_trigger": commit.get("entry_trigger"),
            "catalysts": commit.get("catalysts", []), "risks": commit.get("risks", []),
            "next_earnings_date": commit.get("next_earnings_date"),
            "pead_qualified": probed.get(sym, {}).get("pead_qualified"),  # label gate (P3)
            "washout_reversal": probed.get(sym, {}).get("washout_reversal"),  # shape label
            "iv30": probed.get(sym, {}).get("iv30"),  # entry-time vol context (A12)
            "rvol20": probed.get(sym, {}).get("realized_vol_20d_annual_pct"),
            "never_buy": bool(commit.get("never_buy")),
            "never_buy_reason": commit.get("never_buy_reason"),
        }
    return {**auto_rejected, **results}


MANAGE_TOOLS = ["WebSearch", "WebFetch", "mcp__robinhood-trading__get_equity_quotes"]


def _r1_reject(sym: str, dd: dict) -> dict | None:
    """Return a reject verdict if the quant probe already triggers a hard R1 disqualifier
    (can't price / can't exit).  Only fires on explicit False flags — None means data gap,
    not a disqualifier — so we don't silently block a tradeable name on missing history."""
    flags = dd.get("flags") or {}
    if dd.get("error"):
        reason = f"dd_probe error: {dd['error']}"
    elif flags.get("spread_ok") is False:
        reason = "spread too wide to exit cleanly (flags.spread_ok=false)"
    elif flags.get("liquid") is False:
        reason = "insufficient dollar volume (flags.liquid=false)"
    else:
        return None
    return {"symbol": sym, "decision": "reject", "conviction": None, "dollar_amount": None,
            "hold_intent": None, "thesis_type": "none", "next_earnings_date": "unknown",
            "reason": f"R1 auto-reject: {reason}",
            # r1=True marks this as a TRANSIENT microstructure disqualifier (spread/liquidity), not a
            # thesis. The cache gives r1 verdicts a SHORT TTL (DD_R1_TTL_MIN, ~25m) so a name wide-spread
            # at the open is re-checked within minutes instead of frozen all day under the breakout trigger.
            "r1": True,
            "risks": ["illiquid or wide spread"], "never_buy": False, "never_buy_reason": None,
            "catalysts": []}


def route_book(res: dict, c: dict | None = None) -> str:
    """Two-book split (strategies/two-book-v2-plan.md): label-only routing of a DD verdict.

    'pead' = the measured-edge cohort: the probe-measured gap+vol signal met (pead_qualified
    is True) AND mega-cap (mktcap >= PEAD_BOOK_MIN_MKTCAP_USD — a $30B proxy for the backtest's
    fixed LARGE list, backtest_gap_drift.py:52). Everything else = 'disco' (free-rein
    discretion). Unknown mktcap fails to disco — a name we can't place in the validated regime
    must not enter the evidence cohort. The label NEVER spills: a pead commit that can't be
    funded is skipped by the executor, never re-tagged disco.
    """
    if res.get("pead_qualified") is not True:
        return "disco"
    try:
        floor = float(os.environ.get("PEAD_BOOK_MIN_MKTCAP_USD", "30000000000") or 3e10)
    except ValueError:
        floor = 3e10
    try:
        mktcap = float((c or {}).get("mktcap"))
    except (TypeError, ValueError):
        return "disco"
    return "pead" if mktcap >= floor else "disco"


def run_manage_dd(p: dict, regime: dict, caps: dict, portfolio: dict, dd_model: str) -> dict:
    """Tier-2: re-assess ONE held position on FRESH data + news. Returns
    {action: keep|trim|exit|add, trim_fraction, add_dollars, conviction, hold_intent, reason}.
    Defaults to KEEP on any failure — the hard stop + Tier-1 monitor still protect the downside."""
    sym = str(p.get("symbol", "")).upper().strip()
    try:
        subprocess.run([PYEXE, str(SCRIPTS / "dd_probe.py"), sym],
                       capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as e:
        sys.stderr.write(f"[manage] dd_probe {sym} failed ({e})\n")
    dd_file = TICK / f"dd_{sym}.json"
    try:
        dd = json.loads(dd_file.read_text()) if dd_file.exists() else {"symbol": sym, "error": "no_dd"}
    except (OSError, ValueError):
        dd = {"symbol": sym, "error": "dd_unreadable"}

    cur_val = float(p.get("value") or 0.0)
    headroom = max(0.0, min(caps["MAX_POSITION_USD"] - cur_val,
                            caps["MAX_TOTAL_EXPOSURE_USD"] - portfolio.get("exposure", 0.0),
                            portfolio.get("cash", 0.0)))
    age_h = None
    if p.get("entry_ts"):
        try:
            age_h = round((datetime.now(timezone.utc)
                           - datetime.fromisoformat(p["entry_ts"])).total_seconds() / 3600.0, 1)
        except (ValueError, TypeError):
            age_h = None
    risk = p.get("risk") or {}
    manage_input = json.dumps({
        "symbol": sym,
        "position": {"entry_price": p.get("entry_price"), "qty": p.get("qty"),
                     "current_value_usd": round(cur_val, 2), "last": p.get("last"),
                     "pnl_pct": p.get("pnl_pct"), "age_hours": age_h,
                     "og_conviction": p.get("conviction"), "og_hold_intent": p.get("hold_intent"),
                     "og_thesis": p.get("thesis_type"),
                     "pead_qualified_at_entry": p.get("pead_qualified")},
        "risk": {"score": risk.get("risk"), "band": risk.get("band"), "reasons": risk.get("reasons")},
        "sizing": {"MAX_POSITION_USD": caps["MAX_POSITION_USD"],
                   "available_headroom_usd": round(headroom, 2),
                   "available_cash": round(portfolio.get("cash", 0.0), 2)},
        "regime": regime,
        "prior_evaluation": memory.get_note(sym),
        # Strip TODAY's pead_qualified from the manage packet: it tests today's gap+volume, so a
        # PEAD position held past its gap day reads false forever after — the manage model was
        # misreading that as "thesis invalidated" and exiting day-1 holds (seen live 2026-06-09).
        # The signal class of the ORIGINAL entry rides along as position.pead_qualified_at_entry.
        "dd": {k: v for k, v in dd.items() if k != "pead_qualified"},
    })
    dd_timeout = int(os.environ.get("DD_CLAUDE_TIMEOUT_S", "240"))
    out = run_claude(prompt_text("dd_manage_prompt.txt") + manage_input,
                     dd_model, tools=MANAGE_TOOLS, mcp=True, timeout=dd_timeout, label=f"manage:{sym}")
    if out is None:
        return {"symbol": sym, "action": "keep", "error": "manage_model_failed",
                "reason": "manage model failed -> keep (stop + Tier-1 still protect)"}
    v = extract_decision(out)
    if v.get("_parse_error"):
        return {"symbol": sym, "action": "keep", "error": "manage_parse_error",
                "reason": "unparseable manage output -> keep"}
    action = (v.get("action") or "keep").lower()
    if action not in ("keep", "trim", "exit", "add"):
        action = "keep"
    return {"symbol": sym, "action": action, "trim_fraction": v.get("trim_fraction"),
            "add_dollars": v.get("add_dollars"), "conviction": v.get("conviction"),
            "hold_intent": v.get("hold_intent"), "reason": v.get("reason", ""),
            "risks": v.get("risks", [])}


def main() -> int:
    reset_usage()   # start this tick's token ledger clean
    context = json.loads((TICK / "context_latest.json").read_text())
    caps = context["caps"]
    regime = context.get("regime", {})
    dd_model = os.environ.get("DD_MODEL", "claude-sonnet-4-6")
    # Manage DDs (keep/trim/exit on held positions) use a lighter model by default — the prompt
    # is shorter and the decision is simpler than a fresh entry DD.
    manage_model = os.environ.get("DD_MODEL_MANAGE", "claude-haiku-4-5-20251001")
    max_dd = int(os.environ.get("MAX_DD_CANDIDATES", "2"))

    # --- Stage 1 is now DETERMINISTIC (computed in tick_context.py) — just read it ---
    screen = context.get("screen", {})
    exits = screen.get("exits") or []
    candidates = screen.get("entry_candidates") or []

    # Carry the optional partial-sell fields (qty + scale_tiers) through for scale-out exits; a
    # full close has neither and defaults to selling the whole position in apply_decision.
    actions = [{"symbol": e.get("symbol"), "side": "sell", "reason": e.get("reason", ""),
                **({"qty": e["qty"]} if e.get("qty") is not None else {}),
                **({"scale_tiers": e["scale_tiers"]} if e.get("scale_tiers") else {})}
               for e in exits if e.get("symbol")]

    # --- Stage 2: deep DD + commit, with a per-symbol INDEFINITE cache (re-DD only on a real move) ---
    # A DD is a ~85s Sonnet+web-research call — the tick's dominant token/latency cost. Dynamic
    # discovery keeps surfacing the SAME top movers every 5-min tick, so a short rolling TTL re-burned
    # that call many times a day on names whose thesis hadn't changed. Policy: one fresh DD per name
    # (commit AND reject), reused INDEFINITELY across days, UNTIL the setup materially changes — there is
    # no calendar expiry. (DD_CACHE_INDEFINITE=0 restores the old once-per-ET-day scoping.)
    # The invalidation trigger is ASYMMETRIC by verdict, because the two verdicts go stale differently:
    #   * COMMIT  -> re-DD on a move in EITHER direction past DD_CACHE_DRIFT_PCT. A committed bullish
    #               thesis is broken by a drop AND blown by a run-up (entering late = chasing).
    #   * REJECT  -> re-DD only on an UPSIDE breakout (price up past DD_REJECT_REDD_PCT, or range_pos
    #               pushed into a fresh intraday-high zone). There is no independent news feed in this
    #               repo, so the "fresh catalyst" that should overturn a reject is read from price/range
    #               action — the market revealing it, which is exactly what this gap-drift strategy
    #               trades. A downside continuation only CONFIRMS the reject, so we don't re-burn on it.
    # Execution still re-checks fresh price + caps + allow_entries in apply_decision, so reusing a
    # days-old commit never trades on a stale price. Errors are never cached (retry next tick).
    # DD_CACHE_TTL_MIN (default 0 = no rolling cap) adds an optional rolling-age cap on top; set any
    # trigger %% to 0 to disable it.
    dd_results = []
    cache = load_cache()
    today_et = datetime.now(ET).strftime("%Y-%m-%d")   # cache keyed to the ET trading day
    # INDEFINITE cache (default): a verdict is reused ACROSS days, expiring ONLY when the price-movement
    # trigger below fires (commit = move either way past DD_CACHE_DRIFT_PCT; reject = upside breakout) —
    # not on a calendar rollover. A thesis whose price hasn't moved hasn't changed, so re-DDing it daily
    # just re-burned a ~85s Sonnet+web call for the same answer. Set DD_CACHE_INDEFINITE=0 to restore the
    # old once-a-day scoping. The drift trigger now measures from the ORIGINAL DD price however old, so a
    # name that has run/dropped meaningfully since its verdict still re-DDs on the next tick it resurfaces.
    indefinite = os.environ.get("DD_CACHE_INDEFINITE", "1").strip().lower() not in ("0", "false", "no", "")
    rolling_ttl = int(os.environ.get("DD_CACHE_TTL_MIN", "0")) * 60   # global max verdict age (0 = none)
    # R1 rejects (spread/liquidity) are TRANSIENT — a name wide-spread at the open is often fine later —
    # so they get their own SHORT TTL and are re-checked often instead of frozen under the breakout trigger.
    r1_ttl = int(os.environ.get("DD_R1_TTL_MIN", "25")) * 60
    # A cached COMMIT is re-DD'd mid-day if the live price has moved more than this % (either way)
    # since the DD — the "significant price movement" that invalidates a same-day entry thesis.
    drift_pct = float(os.environ.get("DD_CACHE_DRIFT_PCT", "3.0"))
    # A cached REJECT is overturned only by an UPSIDE breakout since the verdict (no news feed exists,
    # so price/range action is the catalyst proxy): live price up past DD_REJECT_REDD_PCT, OR range_pos
    # pushed into a fresh intraday-high zone (>= DD_REJECT_REDD_RANGE and higher than at reject time).
    reject_redd_pct = float(os.environ.get("DD_REJECT_REDD_PCT", "2.0"))
    reject_redd_range = float(os.environ.get("DD_REJECT_REDD_RANGE", "0.90"))
    now = time.time()
    cache_dirty = False
    # Prune dead entries so the file doesn't grow every session: verdicts stamped for a prior ET day,
    # plus legacy entries (no "day" stamp) past the 24h fallback. Both can no longer ever be a cache
    # hit, so dropping them is loss-free and keeps each tick's read+rewrite of the blob small.
    # In INDEFINITE mode there is no day-based expiry, so day-pruning is skipped — verdicts persist until
    # a price move overturns them (or they're overwritten by a fresh DD on the same name).
    if indefinite:
        stale_keys = []
    else:
        stale_keys = [k for k, e in cache.items()
                      if (e.get("day") not in (None, today_et))
                      or (e.get("day") is None and now - e.get("ts", 0) >= 86400)]
        for k in stale_keys:
            cache.pop(k, None)
    cache_dirty = bool(stale_keys)
    # Async mode: fold any FINISHED background DD verdicts into the cache BEFORE serving cache hits,
    # so a name dispatched a prior tick is acted on the moment its worker lands (decide is the sole
    # cache writer; workers only touch their own job file).
    async_on = _async_on()
    ingested_now = ingest_dd_jobs(cache, today_et, now) if async_on else set()
    if ingested_now:
        cache_dirty = True
    book_full = False
    entry_did_fresh = False   # did the entry pass run any FRESH (uncached) DDs this tick? (gates the manage wave)
    headroom = None
    book_full_note = ""   # set inside the entries block (where portfolio is in scope) if full
    if context.get("allow_entries") and candidates:
        positions_ctx = context.get("positions", [])
        portfolio = {
            "cash": context["portfolio"]["cash"],
            "exposure": context["portfolio"].get("positions_value", 0.0),
            "open_positions": context["portfolio"].get("open_positions", len(positions_ctx)),
            "held": [p["symbol"] for p in positions_ctx],
        }
        # Portfolio-level short-circuit: when the book is full, available headroom (mirrors run_dd's
        # formula) drops below a usable lot and EVERY entry rejects on that single portfolio-wide
        # constraint — nothing to do with the name. Skip Stage-2 entirely so we don't burn N ~85s
        # Sonnet+web DD calls per tick re-rejecting the same top movers; log one "book full" line.
        # Headroom is sized off SETTLED buying power (what the executor can actually spend), NOT NAV
        # cash: NAV can look ample while settled BP is ~0 (unsettled T+1 proceeds), which is exactly
        # when every commit then defers "no settled cash" at execution — a full DD pass burned on names
        # we can't buy. Paper has no settling, so settled_buying_power falls back to full cash there.
        spendable = portfolio["cash"]
        sbp = context["portfolio"].get("settled_buying_power")
        if sbp is not None:
            spendable = sbp
        headroom = max(0.0, min(caps["MAX_POSITION_USD"],
                                caps["MAX_TOTAL_EXPOSURE_USD"] - portfolio["exposure"],
                                spendable))
        min_headroom = float(os.environ.get("MIN_ENTRY_HEADROOM_USD",
                                            caps.get("MIN_POSITION_USD") or 25.0))
        # No-deployable-cash gate (owner: "no cash -> just monitor positions, skip new DD"). The floor
        # is the smallest entry we'd actually place — the lowest conviction tier, 0.35x MAX_POSITION_USD
        # — so a settled balance that can't fund even a minimal bet turns the whole entry/DD pass off
        # while the manage wave below still runs on held positions. Tunable via MIN_ENTRY_SETTLED_USD.
        min_entry_cash = float(os.environ.get("MIN_ENTRY_SETTLED_USD")
                               or round(float(caps["MAX_POSITION_USD"]) * 0.35, 2))
        exp_headroom = caps["MAX_TOTAL_EXPOSURE_USD"] - portfolio["exposure"]
        no_cash = spendable < min_entry_cash and spendable <= exp_headroom
        book_full = headroom < min_headroom or no_cash
        if book_full:
            if no_cash:
                book_full_note = (f"${spendable:.2f} settled cash < ${min_entry_cash:.0f} min entry — "
                                  f"no deployable cash, monitoring positions only")
            else:
                book_full_note = (f"${headroom:.2f} headroom (exposure {portfolio['exposure']:.0f}"
                                  f"/{caps['MAX_TOTAL_EXPOSURE_USD']:.0f}, "
                                  f"{portfolio['open_positions']} pos) < usable lot")

        # Consider EVERY candidate — serving a same-day cached verdict is free (no LLM call), so the
        # per-tick fresh-DD budget (max_dd) must NOT hide a candidate that already has a commit/reject
        # in today's cache. max_dd is applied below, to cache MISSES only.
        shortlist = []
        if not book_full:
            for c in candidates:
                sym = str(c.get("symbol", "")).upper().strip()
                if sym:
                    shortlist.append((sym, c))

        # Serve cached verdicts synchronously: reuse a commit OR reject DD'd EARLIER TODAY (same ET
        # day), unless price has drifted past DD_CACHE_DRIFT_PCT since the verdict. Errors are never
        # cached so they retry. Cache misses need a fresh Stage-2 call and go to the parallel pool.
        cache_hits = {}
        fresh_jobs = []
        for sym, c in shortlist:
            res = None
            cached = cache.get(sym)
            if cached:
                cdec = (cached.get("result") or {}).get("decision")
                age = now - cached.get("ts", 0)
                # Reuse window: INDEFINITE (default) reuses across days — expiry is the price-movement
                # trigger below, not the calendar. With DD_CACHE_INDEFINITE=0, fall back to same-ET-day
                # scoping (ts age for pre-existing entries without a "day" stamp). An optional rolling_ttl
                # can further cap reuse to sub-day if set, in either mode.
                cday = cached.get("day")
                same_day = True if indefinite else ((cday == today_et) if cday else (age < 86400))
                # R1 (spread/liquidity) verdicts get the short r1_ttl; everything else the global rolling_ttl.
                # A 0 TTL means no cap for that class. R1's short cap is what unfreezes a name whose spread
                # has since tightened, without re-probing it every single tick.
                is_r1 = bool((cached.get("result") or {}).get("r1"))
                eff_ttl = r1_ttl if is_r1 else rolling_ttl
                within_rolling = eff_ttl <= 0 or age < eff_ttl
                # Asymmetric re-DD trigger (see policy comment above): commit = move either way;
                # reject = upside breakout only (price OR range_pos). move_pct is signed: + is up.
                ref, cur = cached.get("ref_price"), c.get("last")
                move_pct = (cur / ref - 1) * 100 if (ref and cur) else 0.0
                if cdec == "commit":
                    stale = drift_pct > 0 and abs(move_pct) > drift_pct
                else:  # reject — overturn only on an upside breakout, never on downside follow-through
                    ref_rp, cur_rp = cached.get("ref_range_pos"), c.get("range_pos")
                    broke_up = reject_redd_pct > 0 and move_pct > reject_redd_pct
                    broke_range = bool(ref_rp is not None and cur_rp is not None
                                       and cur_rp >= reject_redd_range and cur_rp > ref_rp)
                    stale = broke_up or broke_range
                servable = cdec in ("commit", "reject") and same_day and within_rolling and not stale
                # Re-DD before BUYING a committed name: only act on a commit verdict that was produced
                # THIS tick (freshly ingested from its async worker, so sym in ingested_now). A commit
                # that's been SITTING in the cache — e.g. parked while we waited for settled cash — is
                # NOT bought on the stale thesis; it falls through to a fresh re-DD and the buy waits for
                # the new verdict. Rejects are still served from cache (no money at risk, nothing to
                # re-confirm). In sync mode ingested_now is empty, so every cached commit re-DDs inline
                # this tick and buys on the fresh result.
                if servable and cdec == "commit" and sym not in ingested_now:
                    servable = False
                if servable:
                    res = {**cached["result"], "cached": True, "cached_age_min": int(age / 60)}
            if res is not None:
                cache_hits[sym] = res
            else:
                fresh_jobs.append((sym, c))
        # max_dd caps only the EXPENSIVE fresh DDs (each ~85s Sonnet+web call); every cache hit above is
        # already served. Only cache MISSES compete for the per-tick budget — the rest defer to a later
        # tick (or get served instantly once today's verdict lands in the cache).
        fresh_jobs = fresh_jobs[:max_dd] if max_dd >= 0 else fresh_jobs
        # ASYNC (DD_ASYNC): do NOT block the tick on entry DDs. Dispatch each cache-miss to a detached
        # worker (skipping any already in flight) up to the global ceiling; its verdict lands in a
        # LATER tick via ingest_dd_jobs above. SYNC (default): run them now in a thread pool and act
        # this tick. fresh_results = what got computed SYNCHRONOUSLY (empty in async mode).
        fresh_results = {}
        if async_on:
            max_inflight = int(os.environ.get("DD_ASYNC_MAX_INFLIGHT", "6"))
            dispatched, in_flight = [], []
            for sym, c in fresh_jobs:
                if job_in_flight(sym, now):
                    in_flight.append(sym)
                    continue
                if count_in_flight(now) >= max_inflight:
                    break  # global cap on concurrent background workers (bounds CPU/RAM + spend)
                if dispatch_dd(sym, c, now):
                    dispatched.append(sym)
            entry_did_fresh = bool(dispatched)   # dispatching IS initiating fresh work -> defer manage
            if dispatched or in_flight:
                print(f"  async DD: +{len(dispatched)} dispatched ({', '.join(dispatched) or '-'}), "
                      f"{len(in_flight)} already in flight, {count_in_flight(now)} running total "
                      f"(verdicts act a later tick)")
        else:
            entry_did_fresh = bool(fresh_jobs)   # ran real FRESH DD work this tick -> defer the manage wave
            # Batch all cache-miss DDs into ONE claude subprocess so system-prompt + tool-schema
            # startup cost is paid once, not N times. Wall-clock is ~sum(per-symbol) rather than
            # max(per-symbol), but that's the correct tradeoff when N is small and token waste dominates.
            if fresh_jobs:
                t0_batch = time.time()
                try:
                    batch_out = run_dd_batch(fresh_jobs, regime, caps, portfolio, dd_model)
                except Exception as e:
                    batch_out = {sym: {"symbol": sym, "decision": "error",
                                       "error": f"dd_batch_exception: {e}", "conviction": None,
                                       "dollar_amount": None, "reason": "", "catalysts": [], "risks": []}
                                 for sym, _ in fresh_jobs}
                elapsed = round(time.time() - t0_batch, 1)
                for sym, _ in fresh_jobs:
                    r = batch_out.get(sym, {"symbol": sym, "decision": "error",
                                            "error": "missing_from_batch", "conviction": None,
                                            "dollar_amount": None, "reason": "", "catalysts": [], "risks": []})
                    fresh_results[sym] = {**r, "dd_elapsed_s": elapsed}

        # Reassemble in screen order: cache + persist fresh commits/rejects, then build buy actions.
        for sym, c in shortlist:
            res = cache_hits.get(sym) or fresh_results.get(sym)
            if res is None:
                continue
            # Two-book label (Phase 0, measurement-only): every verdict — commit AND reject — gets
            # a book so the forward ledger can split the pead cohort from free-rein discretion.
            # setdefault: a verdict cached earlier today keeps the book it was routed to at DD time.
            if res.get("decision") in ("commit", "reject"):
                res.setdefault("book", route_book(res, c))
            # Cache commits and rejects (each reused under its asymmetric trigger + TTL at read time);
            # never cache an error — a transient model/timeout failure must be retried, not frozen. R1
            # rejects ARE cached (res["r1"]) but read back under the short r1_ttl, so a transient spread/
            # liquidity veto is re-checked within ~25 min instead of frozen all day or re-probed every tick.
            if not res.get("cached") and res.get("decision") in ("commit", "reject"):
                cache[sym] = {"ts": now, "day": today_et,          # stamped for age/scoping (reuse is
                                                                   #   indefinite unless DD_CACHE_INDEFINITE=0)
                              "ref_price": c.get("last"),           # price at DD time (drift trigger)
                              "ref_range_pos": c.get("range_pos"),  # range pos at DD time (reject breakout trigger)
                              "result": {k: v for k, v in res.items() if k != "dd_elapsed_s"}}
                cache_dirty = True
                # Persist the main points to long-term memory and auto-exclude a never-buy name.
                memory.record(sym, decision=res["decision"], conviction=res.get("conviction"),
                              reason=res.get("reason", ""), catalysts=res.get("catalysts"),
                              risks=res.get("risks"), next_earnings_date=res.get("next_earnings_date"),
                              never_buy=res.get("never_buy"), never_buy_reason=res.get("never_buy_reason"))
            dd_results.append(res)
            if res.get("decision") == "commit" and res.get("dollar_amount"):
                # Downtrend = PEAD-only mode (regime-split backtest 2026-06-09): in a confirmed SPY
                # downtrend, ONLY commits meeting the measured gap+vol signal trade (suppressing
                # free-rein/cached commits deterministically — the screen filter can't catch cached
                # or flat-gap earnings names), and survivors size down one tier (x0.6): the mean edge
                # holds in downtrends (+1.74% vs +1.49% LARGE) but win rate drops 55%->48%.
                downtrend_mode = bool(screen.get("downtrend_pead_only"))
                if downtrend_mode and res.get("pead_qualified") is not True:
                    print(f"  REGIME-GATE {sym}: commit suppressed — downtrend PEAD-only mode, "
                          f"pead_qualified={res.get('pead_qualified')}")
                    continue
                dollar = res["dollar_amount"]
                tag = f"DD/{res.get('conviction', '?')}" + ("/cached" if res.get("cached") else "")
                if downtrend_mode:
                    dollar = round(dollar * 0.6, 2)
                    tag += "/downtrend-0.6x"
                act = {"symbol": sym, "side": "buy",
                       "dollar_amount": dollar,
                       "conviction": res.get("conviction"),     # OG DD -> persisted on the position
                       "hold_intent": res.get("hold_intent"),   #   so the Tier-1 risk monitor can reason
                       "thesis_type": res.get("catalyst_type"),
                       "pead_qualified": res.get("pead_qualified"),  # measured-signal flag -> trade log (P3)
                       "washout_reversal": res.get("washout_reversal"),  # shape label -> trade log
                       "book": res.get("book", "disco"),        # two-book split: pead | disco (v2 plan)
                       "reason": f"[{tag}] {res.get('reason', '')}"}
                # Optional level-triggered entry: if the DD returned an entry_trigger, ARM it for the
                # sentinel to fire on the cross instead of buying now (LLM arms, fast loop fires). No
                # trigger -> immediate buy (default, unchanged). ENTRY_ARMING=0 forces immediate.
                trig = res.get("entry_trigger")
                arming_on = os.environ.get("ENTRY_ARMING", "1").strip().lower() not in ("0", "false", "no", "")
                if (arming_on and isinstance(trig, dict) and trig.get("price")
                        and str(trig.get("direction")).lower() in ("above", "below")):
                    act["arm"] = True
                    act["entry_trigger"] = {"price": trig["price"],
                                            "direction": str(trig["direction"]).lower()}
                actions.append(act)
        if cache_dirty:
            save_cache(cache)

    # --- Tier-2: risk-adaptive manage-DD on HELD positions that are DUE (riskiest first, capped) ---
    # Each holding carries a risk-adaptive re-DD TTL (hold_risk.py: critical->now, high->5m, med->20m,
    # low->60m). A holding is DUE when that TTL has elapsed since its last manage-DD (or it's critical,
    # or never managed). We re-check only the due ones, on FRESH news, and keep/trim/exit/add. The hard
    # stop + the Tier-1 monitor cover the gaps between reviews. Best-effort: never sink the tick.
    manage_results = []
    try:
        positions_ctx = context.get("positions", [])
        # Interleave the two DD waves: only run the manage wave on a tick that did NOT already run fresh
        # entry DDs, so a single tick never pays for BOTH waves (~2x). Tier-1 protective sells run every
        # tick regardless (tick_context), so a deferred manage-DD never leaves a critical holding unwatched.
        if (context.get("market_open") and not context.get("data_stale") and positions_ctx
                and not entry_did_fresh):
            mcache = load_manage_cache()
            exiting = {a.get("symbol") for a in actions if a.get("side") == "sell"}  # already exiting this tick
            due = []
            for p in positions_ctx:
                sym = p.get("symbol")
                if not sym or sym in exiting:
                    continue
                prisk = p.get("risk") or {}
                ttl_min = float(prisk.get("redd_ttl_min", 20.0))
                ent = mcache.get(sym)
                is_due = (ent is None or prisk.get("band") == "critical"
                          or (now - float(ent.get("ts", 0))) >= ttl_min * 60)
                if is_due:
                    due.append((float(prisk.get("risk", 0) or 0), p))
            due.sort(key=lambda x: -x[0])                       # riskiest holdings first
            max_manage = int(os.environ.get("HOLD_REVIEW_MAX_PER_TICK", "4"))
            due_ps = [p for _, p in due[:max_manage]]
            if due_ps:
                pm = {"cash": context["portfolio"]["cash"],
                      "exposure": context["portfolio"].get("positions_value", 0.0)}
                mfresh = {}
                workers = min(len(due_ps), int(os.environ.get("DD_MAX_PARALLEL", "4")))
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = {ex.submit(run_manage_dd, p, regime, caps, pm, manage_model): p["symbol"]
                            for p in due_ps}
                    for fut in concurrent.futures.as_completed(futs):
                        s = futs[fut]
                        try:
                            mfresh[s] = fut.result()
                        except Exception as e:               # one manage-DD blowing up isn't fatal
                            mfresh[s] = {"symbol": s, "action": "keep",
                                         "error": f"manage_exception: {e}", "reason": ""}
                for p in due_ps:
                    sym = p["symbol"]
                    res = mfresh.get(sym)
                    if not res:
                        continue
                    prisk = p.get("risk") or {}
                    mcache[sym] = {"ts": now, "band": prisk.get("band"),
                                   "ttl_min": prisk.get("redd_ttl_min"), "result": res}
                    manage_results.append(res)
                    act = res.get("action")
                    if act == "exit":
                        actions.append({"symbol": sym, "side": "sell",
                                        "reason": f"[manage/exit] {res.get('reason', '')}"})
                    elif act == "trim" and res.get("trim_fraction"):
                        try:
                            frac = max(0.0, min(1.0, float(res["trim_fraction"])))
                        except (TypeError, ValueError):
                            frac = 0.0
                        lot_qty = float(p.get("qty") or 0.0)
                        qty = round(frac * lot_qty, 6)
                        # Whole-share discipline (live, P2): a partial sell must leave BOTH legs
                        # whole — a fractional remainder is stop-less dust (a 0.35-share SRAD trim
                        # re-created exactly what the dust cleanup removed). Floor the trim to
                        # whole shares; a lot too small to split keeps (exit is the tool for real
                        # deterioration).
                        if (str(context.get("mode", "")).lower() == "live"
                                and lot_qty >= 1 and lot_qty == int(lot_qty)):
                            qty = float(int(qty))
                            if qty < 1 or lot_qty - qty < 1:
                                sys.stderr.write(f"[decide] manage trim {sym} skipped: "
                                                 f"{frac:.0%} of {lot_qty:g} sh can't keep both "
                                                 "legs whole-share (P2)\n")
                                qty = 0.0
                        if qty > 0:
                            actions.append({"symbol": sym, "side": "sell", "qty": qty,
                                            "reason": f"[manage/trim {int(frac * 100)}%] {res.get('reason', '')}"})
                    elif act == "add" and res.get("add_dollars"):
                        try:
                            addd = float(res["add_dollars"])
                        except (TypeError, ValueError):
                            addd = 0.0
                        if addd > 0:
                            actions.append({"symbol": sym, "side": "buy", "dollar_amount": addd,
                                            "conviction": res.get("conviction"),
                                            "hold_intent": res.get("hold_intent"),
                                            "thesis_type": p.get("thesis_type"),
                                            "book": p.get("book") or "disco",  # add-on inherits the lot's book
                                            "reason": f"[manage/add] {res.get('reason', '')}"})
                    # "keep" -> no action (the cache ts is still bumped so it coasts for its TTL)
                save_manage_cache(mcache)
    except Exception as e:  # a manage-pass failure must not sink the tick's entries/exits
        sys.stderr.write(f"[decide] manage pass failed (ignored): {e}\n")

    # Forward filter-lift ledger: record each agent-evaluated gap candidate's verdict (commit/reject)
    # so catalyst_filter_report.py can later join it to the realized N-day drift. Best-effort.
    if dd_results:
        catalyst_log.record_events(context, candidates, dd_results)

    rationale = (f"{len(exits)} rule-exit(s), {len(candidates)} screened candidate(s)"
                 + (" [acute risk-off: entries off]" if screen.get("hostile_regime") else "")
                 + (" [downtrend: PEAD-only entries, sized x0.6]" if screen.get("downtrend_pead_only") else "")
                 + (f" [entries off: {book_full_note}]" if book_full else "")
                 + (" [entries gated: market closed/stale]" if not context.get("allow_entries") else ""))
    decision_out = {
        "actions": actions,
        "rationale": rationale,
        "screen": screen,
        "dd": dd_results,
        "manage": manage_results,        # Tier-2 hold re-assessments (keep/trim/exit/add)
        "token_usage": usage_summary(),  # exact per-tick headless-Claude token + cost rollup
    }
    TICK.mkdir(parents=True, exist_ok=True)
    (TICK / "decision_latest.json").write_text(json.dumps(decision_out, indent=2))

    n_commit = sum(1 for d in dd_results if d["decision"] == "commit")
    n_error = sum(1 for d in dd_results if d["decision"] == "error")
    n_reject = len(dd_results) - n_commit - n_error

    # --- human-readable decision trail: WHY each name did / didn't trade (counts alone hide it) ---
    for e in exits:
        print(f"  EXIT {e.get('symbol', '?')}: {e.get('reason', '')}")
    if candidates:
        print(f"screen: {len(exits)} exit(s), {len(candidates)} candidate(s): "
              + ", ".join(fmt_candidate(c) for c in candidates))
        if book_full:
            print(f"  ENTRIES OFF: {book_full_note} — Stage-2 entries skipped this tick")
    else:
        # No candidates -> say why (gate / regime / nothing cleared the bar), not just "0".
        if not context.get("allow_entries"):
            note = f" [entries gated: {context.get('stale_reason') or 'market closed/stale'}]"
        elif screen.get("hostile_regime"):
            note = " [hostile regime: entries off]"
        else:
            note = " [no mover cleared the screen]"
        print(f"screen: {len(exits)} exit(s), 0 candidate(s){note}")
    for d in dd_results:
        print(fmt_dd_line(d))
    # Cache hits are all in dd_results, so this only counts cache-MISS candidates that didn't produce
    # a verdict THIS tick (book-full has its own line above and is excluded here). The reason differs by
    # mode: ASYNC dispatches every miss to a background worker whose verdict lands a LATER tick (so this
    # is normal handoff, NOT the max_dd cap throttling — that cap rarely binds once async decouples DD
    # from tick latency); SYNC truncates the fresh batch at MAX_DD_CANDIDATES.
    not_dd = len(candidates) - len(dd_results)
    if context.get("allow_entries") and not book_full and not_dd > 0:
        if async_on:
            print(f"  (+{not_dd} candidate(s) pending async DD — cache-misses run in detached "
                  f"workers; verdicts act a later tick, cached verdicts always served)")
        else:
            print(f"  (+{not_dd} candidate(s) deferred — MAX_DD_CANDIDATES={max_dd} caps FRESH DDs/tick; "
                  f"cached verdicts always served)")
    print(f"DD: {n_commit} commit / {n_reject} reject / {n_error} error -> {len(actions)} action(s)")
    for m in manage_results:
        print(f"  MANAGE {m.get('symbol')}: {(m.get('action') or '?').upper()} — "
              f"{(m.get('reason') or m.get('error') or '')[:90]}")
    if manage_results:
        n_act = sum(1 for m in manage_results if m.get('action') in ('trim', 'exit', 'add'))
        print(f"MANAGE: {len(manage_results)} holding(s) re-checked, {n_act} acted")
    tu = decision_out["token_usage"]
    if tu["n_calls"]:
        print(f"TOKENS: {tu['n_calls']} call(s), {tu['total_tokens']:,} tok "
              f"(in {tu['input_tokens']:,} / out {tu['output_tokens']:,} / "
              f"cache_read {tu['cache_read_input_tokens']:,}) ~${tu['cost_usd']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
