#!/usr/bin/env python3
"""
Extract all agent prompts + results from a workflow run and save to a readable file.

Usage:
    python3 scripts/save_workflow_research.py <workflow_run_id> [output_path]

Example:
    python3 scripts/save_workflow_research.py wf_5e610169-6e0
    python3 scripts/save_workflow_research.py wf_5e610169-6e0 data/research/best-traders.md
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime

TRANSCRIPTS_BASE = Path.home() / ".claude/projects/-Users-mcosta-agentic-trading"


def find_workflow_dir(run_id: str) -> Path | None:
    for session_dir in TRANSCRIPTS_BASE.iterdir():
        if not session_dir.is_dir():
            continue
        wf_dir = session_dir / "subagents/workflows" / run_id
        if wf_dir.exists():
            return wf_dir
    return None


def extract_agent(jsonl_path: Path) -> dict:
    try:
        lines = [json.loads(l) for l in jsonl_path.read_text().splitlines() if l.strip()]
    except Exception as e:
        return {"error": str(e)}

    prompt = ""
    result = ""
    tool_outputs = []

    for line in lines:
        t = line.get("type")

        if t == "user" and not prompt:
            msg = line.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        prompt = c["text"]
                        break
            elif isinstance(content, str):
                prompt = content

        elif t == "assistant":
            msg = line.get("message", {})
            content = msg.get("content", [])
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict):
                        if c.get("type") == "text" and c.get("text"):
                            result = c["text"]
                        elif c.get("type") == "tool_use":
                            # capture StructuredOutput / WebSearch calls
                            tool_outputs.append({
                                "tool": c.get("name", ""),
                                "input": c.get("input", {}),
                            })

    return {"prompt": prompt, "result": result, "tool_outputs": tool_outputs}


def classify_agent(prompt: str) -> str:
    p = prompt.lower()
    if "scope" in p or "decompose" in p or "search angle" in p:
        return "Scope"
    if "web search" in p or "search for" in p or "find sources" in p:
        return "Search"
    if "fetch" in p or "extract" in p or "read the page" in p or "url" in p:
        return "Fetch"
    if "adversarial" in p or "refut" in p or "skepti" in p or "voter" in p:
        return "Verify"
    if "synthesi" in p or "merge" in p or "final report" in p or "cite" in p:
        return "Synthesize"
    return "Other"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    run_id = sys.argv[1]
    wf_dir = find_workflow_dir(run_id)
    if not wf_dir:
        print(f"ERROR: workflow directory not found for {run_id}", file=sys.stderr)
        sys.exit(1)

    jsonl_files = sorted(wf_dir.glob("agent-*.jsonl"))
    print(f"Found {len(jsonl_files)} agent files in {wf_dir}", file=sys.stderr)

    agents = []
    for f in jsonl_files:
        agent_id = f.stem  # agent-xxxx
        data = extract_agent(f)
        data["id"] = agent_id
        data["phase"] = classify_agent(data.get("prompt", ""))
        agents.append(data)

    # Group by phase
    phases = ["Scope", "Search", "Fetch", "Verify", "Synthesize", "Other"]
    grouped = {p: [a for a in agents if a["phase"] == p] for p in phases}

    # Output path
    if len(sys.argv) >= 3:
        out_path = Path(sys.argv[2])
    else:
        out_path = Path("data/research") / f"workflow-{run_id}.md"

    out_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Workflow Research: {run_id}",
        f"",
        f"Extracted: {now}  |  Agents: {len(agents)}",
        f"",
        f"**Question:** What do the best discretionary and systematic traders (hedge funds, prop shops, elite retail)"
        f" actually do differently — entry discipline, position sizing, stop management, holding periods,"
        f" portfolio construction, and process — and how could an agentic momentum trading system on Robinhood"
        f" (cash account, PEAD/catalyst gap-drift signal, 5-20 day hold) incorporate those practices?",
        f"",
        "---",
        "",
    ]

    for phase in phases:
        group = grouped.get(phase, [])
        if not group:
            continue
        lines.append(f"## Phase: {phase} ({len(group)} agents)")
        lines.append("")
        for i, a in enumerate(group, 1):
            lines.append(f"### {phase} {i} — `{a['id']}`")
            lines.append("")
            if a.get("prompt"):
                lines.append("**Prompt:**")
                lines.append("")
                lines.append("```")
                lines.append(a["prompt"][:3000])
                if len(a["prompt"]) > 3000:
                    lines.append(f"... [{len(a['prompt']) - 3000} chars truncated]")
                lines.append("```")
                lines.append("")
            if a.get("tool_outputs"):
                lines.append("**Tool calls:**")
                lines.append("")
                for t in a["tool_outputs"][:10]:
                    inp = t["input"]
                    if t["tool"] in ("WebSearch", "web_search"):
                        lines.append(f"- WebSearch: `{inp.get('query', inp)}`")
                    elif "StructuredOutput" in t["tool"]:
                        lines.append(f"- StructuredOutput (result captured below)")
                    else:
                        lines.append(f"- {t['tool']}: {str(inp)[:200]}")
                lines.append("")
            if a.get("result"):
                lines.append("**Result:**")
                lines.append("")
                lines.append(a["result"][:6000])
                if len(a["result"]) > 6000:
                    lines.append(f"\n... [{len(a['result']) - 6000} chars truncated]")
                lines.append("")
            if a.get("error"):
                lines.append(f"**Error:** {a['error']}")
                lines.append("")
            lines.append("---")
            lines.append("")

    out_path.write_text("\n".join(lines))
    print(f"Saved to: {out_path}")
    print(f"  {len(agents)} agents | phases: { {p: len(grouped[p]) for p in phases if grouped[p]} }")


if __name__ == "__main__":
    main()
