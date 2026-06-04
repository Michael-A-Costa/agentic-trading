#!/usr/bin/env python3
"""
stock_memory.py — long-term, per-symbol evaluation memory + a permanent never-buy exclusion list.

Distinct from the short-TTL DD cache (data/dd_cache.json, "don't re-DD for minutes"): this is the
engine's accumulated KNOWLEDGE about names it has researched. Every Stage-2 DD writes the main points
here; the next DD on that name is PRIMED with them, so re-evaluations are faster and consistent ("we
rejected this 3x for dilution — re-confirm or say what changed"). Names DD flags as structurally
uninvestable (fraud, going-concern, serial diluter, delisting) land on the EXCLUSION list and are
filtered out before discovery/DD ever touch them again — never quoted, never researched.

Storage: data/stock_memory.json (gitignored runtime state). Atomic writes; single-flight ticks mean
no concurrent writers.

  {
    "version": 1,
    "stocks": { "SYM": {first_eval, last_eval, n_evals, decision, conviction, summary,
                        catalysts[], risks[], next_earnings_date, recent[{ts,decision,reason}]} },
    "exclusions": { "SYM": {reason, ts, source} }
  }

Gated by STOCK_MEMORY_ENABLED (default 1). When off, get_note() returns None, record() no-ops, and
excluded_symbols() is empty — so the rest of the pipeline transparently ignores memory.

CLI:
  stock_memory.py                      # summary + exclusion list
  stock_memory.py show SYM             # one symbol's memory
  stock_memory.py exclude SYM "reason" # manually add a never-buy name
  stock_memory.py allow SYM            # remove a name from the exclusion list
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MEMORY_PATH = REPO / "data" / "stock_memory.json"
RECENT_KEEP = 5      # verdict-history entries kept per symbol
LIST_TRIM = 3        # catalysts / risks kept per symbol
SUMMARY_TRIM = 240   # chars of the summary line


def _enabled() -> bool:
    return os.environ.get("STOCK_MEMORY_ENABLED", "1") == "1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> dict:
    try:
        m = json.loads(MEMORY_PATH.read_text())
    except (OSError, ValueError):
        m = {}
    m.setdefault("version", 1)
    m.setdefault("stocks", {})
    m.setdefault("exclusions", {})
    return m


def _save(mem: dict) -> None:
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MEMORY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(mem, indent=2))
    os.replace(tmp, MEMORY_PATH)


# --------------------------------------------------------------------------- read (priming)
def get_note(sym: str) -> dict | None:
    """Compact prior-evaluation summary to PRIME the next DD (None if unseen / memory off)."""
    if not _enabled() or not sym:
        return None
    s = _load()["stocks"].get(sym.upper())
    if not s:
        return None
    return {
        "last_eval": s.get("last_eval"),
        "n_evals": s.get("n_evals"),
        "last_decision": s.get("decision"),
        "summary": s.get("summary"),
        "key_risks": (s.get("risks") or [])[:LIST_TRIM],
        "next_earnings_date": s.get("next_earnings_date"),
    }


# --------------------------------------------------------------------------- write (after a DD)
def record(sym: str, *, decision: str, conviction=None, reason: str = "", catalysts=None,
           risks=None, next_earnings_date=None, never_buy: bool = False, never_buy_reason=None) -> None:
    """Persist the main points of one DD; auto-exclude the name if DD flagged it never-buy.

    Call only for real verdicts (commit / reject) — never for an error (no verdict to remember).
    """
    if not _enabled() or not sym:
        return
    sym = sym.upper()
    now = _now()
    mem = _load()
    s = mem["stocks"].get(sym) or {"first_eval": now, "n_evals": 0, "recent": []}
    s["last_eval"] = now
    s["n_evals"] = int(s.get("n_evals", 0)) + 1
    s["decision"] = decision
    s["conviction"] = conviction
    s["summary"] = (reason or "")[:SUMMARY_TRIM]
    if catalysts:
        s["catalysts"] = list(catalysts)[:LIST_TRIM]
    if risks:
        s["risks"] = list(risks)[:LIST_TRIM]
    if next_earnings_date:
        s["next_earnings_date"] = next_earnings_date
    s["recent"] = ([{"ts": now, "decision": decision, "reason": (reason or "")[:120]}]
                   + list(s.get("recent", [])))[:RECENT_KEEP]
    mem["stocks"][sym] = s
    if never_buy:
        mem["exclusions"][sym] = {"reason": (never_buy_reason or reason or "structural disqualifier")[:240],
                                  "ts": now, "source": "auto"}
    _save(mem)


# --------------------------------------------------------------------------- exclusions
def excluded_symbols() -> set[str]:
    """The permanent never-buy set (empty when memory is off)."""
    if not _enabled():
        return set()
    return set(_load()["exclusions"].keys())


def exclusion_reason(sym: str) -> str | None:
    if not _enabled() or not sym:
        return None
    e = _load()["exclusions"].get(sym.upper())
    return e.get("reason") if e else None


def exclude(sym: str, reason: str, source: str = "manual") -> None:
    sym = sym.upper()
    mem = _load()
    mem["exclusions"][sym] = {"reason": reason[:240], "ts": _now(), "source": source}
    _save(mem)


def allow(sym: str) -> bool:
    """Remove a name from the exclusion list. Returns True if it was excluded."""
    sym = sym.upper()
    mem = _load()
    if sym in mem["exclusions"]:
        del mem["exclusions"][sym]
        _save(mem)
        return True
    return False


# --------------------------------------------------------------------------- CLI
def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "show":
        mem = _load()
        sym = argv[2].upper() if len(argv) > 2 else ""
        print(json.dumps(mem["stocks"].get(sym, {"note": f"no memory for {sym}"}), indent=2))
        if sym in mem["exclusions"]:
            print("EXCLUDED:", json.dumps(mem["exclusions"][sym]))
        return 0
    if len(argv) >= 3 and argv[1] == "exclude":
        exclude(argv[2], argv[3] if len(argv) > 3 else "manual exclusion", source="manual")
        print(f"excluded {argv[2].upper()}")
        return 0
    if len(argv) >= 3 and argv[1] == "allow":
        print(f"{'un-excluded' if allow(argv[2]) else 'was not excluded'} {argv[2].upper()}")
        return 0
    mem = _load()
    print(f"stock memory: {len(mem['stocks'])} names evaluated, {len(mem['exclusions'])} excluded "
          f"(enabled={_enabled()})")
    for sym, e in sorted(mem["exclusions"].items()):
        print(f"  EXCLUDE {sym:6} [{e.get('source')}] {e.get('reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
