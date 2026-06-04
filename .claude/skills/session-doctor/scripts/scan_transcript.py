#!/usr/bin/env python3
"""Scan a Claude Code session transcript for mechanical process anomalies.

This surfaces the things a machine can find reliably so the reviewer doesn't have
to re-read a giant transcript token by token:
  - tool results flagged as errors (is_error) or interrupted/denied calls
  - identical commands run several times (a retry-loop signature)
  - user messages that look like corrections/redirects after an assistant turn
    (a proxy for "wrong-but-successful" invocations the model can't otherwise see)
  - the git branch(es) the work happened on (so branch-naming rules can be checked)

It deliberately does NOT judge intent or whether something was later fixed — that's
the model's job using the in-context conversation. Errors from subagent sidechains
are tagged so they aren't conflated with the main thread.

Usage:
    python3 scan_transcript.py                 # auto-find current session
    python3 scan_transcript.py --transcript /path/to/session.jsonl
    python3 scan_transcript.py --cwd /some/project   # resolve that project's session
    python3 scan_transcript.py --json          # machine-readable output
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

PERMISSION_RE = re.compile(
    r"permission|denied|don'?t want to|doesn'?t want to|not allowed|"
    r"requires approval|rejected|declined",
    re.IGNORECASE,
)
NOT_FOUND_RE = re.compile(
    r"no such file|not found|cannot find|does not exist|command not found",
    re.IGNORECASE,
)
# Conservative — strong redirect phrases only (no overloaded "undo"/"revert", which
# are normal dev instructions). Findings are labelled "possible" and only point the
# reviewer at a spot to read. Applied solely to TERSE messages (see CORRECTION_MAX_LEN):
# a real redirect is short, so a long pasted doc that merely contains "stop" is ignored.
CORRECTION_RE = re.compile(
    r"\b(stop|that'?s wrong|that is wrong|that'?s not right|"
    r"not what i|that'?s incorrect|you (broke|missed|forgot|shouldn'?t)|"
    r"why did you|don'?t do that|do not do that|wrong (file|branch|repo|command))\b",
    re.IGNORECASE,
)
CORRECTION_MAX_LEN = 280
INTERRUPT_RE = re.compile(r"\[Request interrupted", re.IGNORECASE)


def project_slug(cwd: str) -> str:
    """Claude Code names project transcript dirs by slugifying the abs path."""
    return re.sub(r"[^A-Za-z0-9-]", "-", cwd)


def find_transcript(explicit: str | None, cwd: str) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() else None

    projects = Path.home() / ".claude" / "projects"
    candidates: list[Path] = []

    slug_dir = projects / project_slug(os.path.abspath(cwd))
    if slug_dir.is_dir():
        candidates = list(slug_dir.glob("*.jsonl"))

    # Fall back to the most recent transcript anywhere if the slug guess missed.
    if not candidates and projects.is_dir():
        candidates = list(projects.glob("*/*.jsonl"))

    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def normalize_text(content) -> str:
    """tool_result.content may be a string or a list of {type,text} blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text") or block.get("content") or "")
            else:
                parts.append(str(block))
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def genuine_user_text(content) -> str | None:
    """Return concatenated text for a real user prompt, or None if this 'user'
    line is just a tool_result carrier or a slash-command/meta envelope."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None  # tool output, not a human turn
        text = " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        return None
    text = text.strip()
    if not text:
        return None
    # Skip slash-command / local-command envelopes and system reminders.
    if text.startswith("<") or "<command-name>" in text or "<local-command" in text:
        return None
    return text


def summarize_input(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return str(inp)[:200]
    if name == "Bash":
        return (inp.get("command") or "").strip()
    if name in ("Edit", "Write", "Read", "NotebookEdit"):
        return inp.get("file_path") or inp.get("notebook_path") or ""
    if name in ("Glob", "Grep"):
        return inp.get("pattern") or ""
    if name in ("Task", "Agent"):
        return inp.get("description") or inp.get("subagent_type") or ""
    if name == "Skill":
        return inp.get("skill") or ""
    return json.dumps(inp, default=str)[:200]


def categorize(text: str, interrupted: bool) -> str:
    if interrupted:
        return "interrupted/denied"
    if PERMISSION_RE.search(text):
        return "permission"
    if NOT_FOUND_RE.search(text):
        return "not-found"
    return "error"


def scan(path: Path, text_limit: int):
    tool_uses: dict[str, dict] = {}   # id -> {name, input, seq, sidechain}
    results: list[dict] = []          # ordered error/interrupted results
    by_name: dict[str, int] = {}
    bash_commands: dict[str, list[int]] = {}
    git_branches: dict[str, int] = {}
    corrections: list[dict] = []      # possible user redirects
    interrupts = 0
    total_events = 0
    total_tool_calls = 0
    seq = 0
    seen_assistant = False

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            total_events += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = obj.get("type")
            sidechain = bool(obj.get("isSidechain"))
            branch = obj.get("gitBranch")
            if branch:
                git_branches[branch] = git_branches.get(branch, 0) + 1

            msg = obj.get("message") or {}
            content = msg.get("content")
            tur = obj.get("toolUseResult")
            interrupted_flag = isinstance(tur, dict) and bool(tur.get("interrupted"))

            if etype == "assistant":
                seen_assistant = True

            # User redirect / interrupt detection on genuine human turns.
            if etype == "user":
                utext = genuine_user_text(content)
                if utext:
                    if INTERRUPT_RE.search(utext):
                        interrupts += 1
                    elif (seen_assistant and len(utext) <= CORRECTION_MAX_LEN
                          and CORRECTION_RE.search(utext)):
                        corrections.append({
                            "after_seq": seq,
                            "text": utext[:text_limit],
                        })

            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")

                if btype == "tool_use":
                    seq += 1
                    total_tool_calls += 1
                    name = block.get("name", "?")
                    inp = block.get("input", {})
                    tool_uses[block.get("id", f"_{seq}")] = {
                        "name": name,
                        "input": inp,
                        "seq": seq,
                        "sidechain": sidechain,
                    }
                    by_name[name] = by_name.get(name, 0) + 1
                    if name == "Bash":
                        cmd = (inp.get("command") or "").strip() if isinstance(inp, dict) else ""
                        if cmd:
                            bash_commands.setdefault(cmd, []).append(seq)

                elif btype == "tool_result":
                    is_error = bool(block.get("is_error"))
                    if not (is_error or interrupted_flag):
                        continue
                    tuid = block.get("tool_use_id", "")
                    use = tool_uses.get(tuid, {})
                    text = normalize_text(block.get("content"))
                    if interrupted_flag and isinstance(tur, dict) and tur.get("stderr"):
                        text = (text + "\n" + str(tur.get("stderr"))).strip()
                    results.append({
                        "seq": use.get("seq"),
                        "tool": use.get("name", "?"),
                        "invocation": summarize_input(use.get("name", "?"), use.get("input", {})),
                        "category": categorize(text, interrupted_flag),
                        "sidechain": use.get("sidechain", sidechain),
                        "error": text[:text_limit],
                    })

    retries = {cmd: seqs for cmd, seqs in bash_commands.items() if len(seqs) > 1}

    return {
        "transcript": str(path),
        "total_events": total_events,
        "total_tool_calls": total_tool_calls,
        "tool_calls_by_name": dict(sorted(by_name.items(), key=lambda kv: -kv[1])),
        "git_branches": dict(sorted(git_branches.items(), key=lambda kv: -kv[1])),
        "user_interrupts": interrupts,
        "possible_corrections": corrections,
        "error_count": len(results),
        "errors": results,
        "repeated_bash_commands": retries,
    }


def render_markdown(data: dict) -> str:
    out = []
    out.append(f"# Session scan — {data['transcript']}")
    out.append("")
    out.append(
        f"**{data['total_tool_calls']} tool calls** across {data['total_events']} events · "
        f"**{data['error_count']} errored/interrupted** · "
        f"**{data['user_interrupts']} user interrupts** · "
        f"**{len(data['possible_corrections'])} possible corrections**"
    )
    if data["tool_calls_by_name"]:
        breakdown = ", ".join(f"{n}×{c}" for n, c in data["tool_calls_by_name"].items())
        out.append(f"_By tool:_ {breakdown}")
    if data["git_branches"]:
        branches = ", ".join(data["git_branches"].keys())
        out.append(f"_Branch(es):_ {branches}  ← check against the project's branch-naming rule")
    out.append("")

    out.append("## Errored / interrupted tool calls")
    if data["errors"]:
        for e in data["errors"]:
            seq = e["seq"] if e["seq"] is not None else "?"
            tag = " [subagent]" if e.get("sidechain") else ""
            out.append(f"- **#{seq} {e['tool']}**{tag} _({e['category']})_ — `{e['invocation'][:160]}`")
            err = (e["error"] or "").strip().replace("\n", " ")
            if err:
                out.append(f"  - ↳ {err[:400]}")
    else:
        out.append("_None — no tool result was flagged as an error or interruption._")
    out.append("")

    if data["possible_corrections"]:
        out.append("## Possible user corrections (verify — strongest hint of a wrong-but-successful action)")
        for c in data["possible_corrections"]:
            snippet = c["text"].strip().replace("\n", " ")
            out.append(f"- after call #{c['after_seq']}: \"{snippet[:200]}\"")
        out.append("")

    if data["repeated_bash_commands"]:
        out.append("## Repeated Bash commands (possible retry loop — verify each was resolved)")
        for cmd, seqs in data["repeated_bash_commands"].items():
            out.append(f"- ran {len(seqs)}× (calls {seqs}): `{cmd[:160]}`")
        out.append("")

    out.append(
        "_Mechanical scan only. Wrong-but-successful invocations and abandoned goals "
        "may not appear here — the corrections list is a hint, not a guarantee. Judge "
        "intent and whether each issue was later resolved from the conversation itself._"
    )
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--transcript", help="explicit path to a .jsonl session transcript")
    ap.add_argument("--cwd", default=os.getcwd(), help="project dir to resolve the session for")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    ap.add_argument("--text-limit", type=int, default=600, help="max chars of each error message")
    args = ap.parse_args()

    path = find_transcript(args.transcript, args.cwd)
    if path is None:
        msg = "Could not locate a session transcript. Pass --transcript <path>."
        print(json.dumps({"error": msg}) if args.json else f"⚠️  {msg}", file=sys.stderr)
        sys.exit(2)

    data = scan(path, args.text_limit)
    print(json.dumps(data, indent=2, default=str) if args.json else render_markdown(data))


if __name__ == "__main__":
    main()
