---
name: session-doctor
description: Review the CURRENT Claude Code session/conversation for the assistant's own process mistakes — tool calls that errored out, wrong or abandoned invocations, retry loops, interrupted/denied calls, and actions taken against the project's CLAUDE.md rules — then auto-fix the safe ones and ask before anything risky. Use this whenever the user asks to "review the session", "check what went wrong", "did you mess anything up", "clean up after yourself", "fix any errors from earlier", "audit this conversation", or wants a retro on the work just done. This audits Claude's own execution this session; it is NOT a PR/code-diff review (use /code-review or /review for that).
---

# /session-doctor — Review this session for process mistakes and fix them

This skill points Claude back at its own session. Over a long working session, tool
calls error out, a command runs against the wrong path, an edit half-lands, a retry
masks an unresolved failure, or something slips past a project rule — and it's easy
for those to get buried under later work. This skill finds them and fixes what's
safe to fix.

Scope is the **Claude Code process itself** — what the assistant did this session —
not the quality of a feature or a PR diff. If the user wants a code review of a
diff/PR, that's `/code-review` or `/review`, not this.

## Usage

```
/session-doctor              # review the current session, auto-fix safe issues, ask on risky
/session-doctor --report     # report findings only, change nothing
/session-doctor --transcript <path.jsonl>   # review a specific session transcript
```

## Step 1 — Get the mechanical signal

Run the bundled scanner. It parses the session transcript on disk and surfaces what
a machine finds reliably:

- **errored / interrupted tool calls** (`is_error`, or interrupted/denied), with
  subagent-sidechain errors tagged separately so they aren't blamed on the main thread
- **repeated identical Bash commands** — a retry-loop signature
- **possible user corrections** — terse user messages after an assistant turn that
  read like a redirect ("stop, wrong branch"). This is a *proxy* for the
  wrong-but-successful invocations the scanner otherwise can't see — treat it as a
  pointer to a spot worth reading, not a confirmed finding
- **the git branch(es)** the work ran on — so branch-naming rules can be checked

```bash
python3 .claude/skills/session-doctor/scripts/scan_transcript.py
```

It auto-locates the current session (most recently modified transcript for this
project). Pass `--transcript <path>` to target another, or `--json` if you want to
post-process. If it can't find a transcript, fall back to Step 2 alone — the
in-context conversation is enough to work from.

The scanner is a net for mechanical failures and hints. It still can't *judge*: a
command that exited 0 but did the wrong thing, a rule violation, an abandoned goal,
or whether an error was later fixed. Those come from Step 2.

## Step 2 — Read the session with your own eyes

The scanner finds errors; you find mistakes. Walk back through this session's
conversation and tool calls and look for things the scanner can't:

- **Wrong invocations that "succeeded"** — wrong file path, wrong flags, wrong repo,
  a `sed`/`grep` that matched nothing, an edit applied to the wrong block, a command
  that did something subtly different from what was intended. The scanner's "possible
  user corrections" are your best lead here: each one is a place where the user felt
  the need to redirect — read what came just before it and find what triggered it.
- **Errors that were never actually resolved** — cross-check each scanner finding:
  was it fixed later in the session, or just left behind? Only unresolved ones count.
  When something did go wrong, trace back to the *earliest* point things diverged and
  fix the root cause, not the last visible symptom — a later error is often the echo
  of an earlier wrong turn, and patching only the symptom leaves the real bug in place.
- **Retry loops** — the same failing approach tried repeatedly without addressing the
  root cause. (The scanner flags repeated Bash commands; confirm whether the repeat
  was a real fix or spinning.)
- **Project-rule violations** — read the active `CLAUDE.md` (and `MEMORY.md` if
  present) and check the session against its rules. In this repo that includes things
  like: branch names must be the ticket key only; commit messages must start with the
  ticket ID and never mention AI; never `cd` combined with output redirection; never
  transition/comment a JIRA ticket or comment on a PR unless explicitly asked; always
  set Developer when assigning; push after committing. Don't hardcode this list — read
  what the project actually documents and check against that.
- **Half-finished mechanics** — a branch created but never pushed, a file written but
  a follow-up edit skipped, a tool call that errored and the goal silently dropped.

Be honest and precise. **Do not manufacture problems.** A clean session is a valid
and common outcome — if nothing actually went wrong, say so plainly rather than
inventing minor nitpicks. The value here is catching real mistakes, not padding a list.

## Step 3 — Triage each finding: safe vs. risky

For every real issue, decide whether its fix is safe to apply automatically or needs
the user's sign-off first. The line is **reversibility and reach** — does the fix stay
local and undoable, or does it leave the machine / do something hard to take back?

**Safe — apply automatically (Step 4a):**
- Re-running a read-only command with the corrected path/flags
- Correcting a local file edit (fixing a wrong path, re-applying an edit that didn't
  land, fixing a syntax error or wrong block introduced earlier this session)
- Re-running a failed *local* build/test/lint command after fixing its cause
- Anything confined to the working tree that git can trivially undo

**Risky — describe and ask first (Step 4b):** anything outward-facing or hard to
reverse. This intentionally mirrors the repo's Action Guards:
- `git push`, `git commit`, branch deletion, history rewrites
- JIRA transitions or comments; Bitbucket PR comments/approvals
- Sending email, posting to chat, any network mutation
- Database writes, `rm`/deletions, moving files out of the tree
- Anything where you're unsure — when in doubt, treat it as risky and ask

`--report` mode: skip Step 4a entirely. List everything (safe and risky) as proposals
and change nothing.

## Step 4a — Apply the safe fixes

Make the safe corrections directly. For each one, keep a one-line note of what you did
so it lands in the final report. Re-verify where cheap (re-run the test, re-read the
corrected file) so the report can state the fix actually worked rather than assuming it.

## Step 4b — Surface the risky ones for approval

For each risky item, present: what went wrong, the evidence (the tool call + error or
the rule it crossed), and the exact fix you propose (the precise command, comment text,
or transition). Then stop and let the user decide. Don't bundle several risky actions
into one yes/no — list them so the user can approve selectively.

## Step 5 — Report

Close with a compact summary using this structure:

```
## Session review

**Scanned:** <N> tool calls · <M> errored/interrupted · <K> rule checks

### Fixed automatically
- <what was wrong> → <what I did> (verified: <how / "not verified">)
   (or: "Nothing needed auto-fixing.")

### Needs your approval
1. <what's wrong> — evidence: <tool/error/rule>
   Proposed fix: <exact command / comment / transition>
   (or: "Nothing risky to approve.")

### Clean
- <areas checked that were fine — keep brief>
```

If the whole session was clean, say so directly: report the scan counts and a short
"no unresolved process issues found" rather than stretching for findings.
