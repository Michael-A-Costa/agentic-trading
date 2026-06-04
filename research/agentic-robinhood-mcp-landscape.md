# How People Are Using the Robinhood Agentic MCP Trader

*Research compiled 2026-06-03. Sources: 7 (2 journalism articles, 1 skeptic article, 1 builder's-log
analysis, official Robinhood docs, and 3 first-hand practitioner accounts). Links at the bottom.*

---

## TL;DR

- Robinhood launched **Agentic Trading** (announced **May 27, 2026**, trading live ~**June 2**) — an
  official **MCP server** (`https://agent.robinhood.com/mcp/trading`) that lets *any* MCP-capable agent
  (Claude, ChatGPT, Codex, Cursor) trade **equities** in a **ring-fenced "agentic" account**. This is
  the one **we are already wired into** (`.mcp.json`).
- There are **two parallel ecosystems**, and the sources blur them — keep them separate:
  1. **Official RH MCP** (what we use): isolated account, read-only on everything else, kill switch,
     trade-preview, per-trade push notifications, equities-only beta. **No credential handling by us** —
     auth is Robinhood's own flow.
  2. **Third-party DIY MCPs** (`trayd-mcp`, `verygoodplugins/robinhood-mcp`, etc.): unofficial, wrap the
     private Robinhood API, and **pass your email/password through a third-party server** — the source of
     most of the security alarm online. **We are NOT using these.**
- **What real people actually do**: natural-language rules ("buy $100 of X when it drops 2%"), small-account
  **1%-target momentum bots** run on a scheduler, ladder-limit-order automation, and "investment theme"
  rebalancing. Most are **tiny accounts** ($100-ish) treated as experiments.
- **The honest catch**: Robinhood disclaims all liability; AI can misread/act on stale data; and at very
  small capital the **spread + noise eats the edge**. The skeptics (Motley Fool, Hacker News) are about
  *fundamentals* and *credential security*, not the trading mechanics.

---

## The two ecosystems (don't conflate them)

| | **Official Robinhood Agentic MCP** (ours) | **Third-party DIY MCPs** |
|---|---|---|
| Endpoint | `agent.robinhood.com/mcp/trading` | e.g. `mcp.trayd.ai/mcp`, self-hosted |
| Auth | Robinhood's own onboarding + agentic account | Your RH **email/password through their server** |
| Account | Dedicated, isolated "agentic" account | Your real primary account |
| Safety | Kill switch, isolated funds, trade preview, per-trade alerts, read-only elsewhere | Whatever the author built; tokens in-RAM at best |
| Assets | Equities only (beta) | Whatever the private API exposes |
| Status | Official product, beta rollout | Community projects |

We're on the left column — structurally the safer one. The Hacker News security backlash ("*Your Robinhood
email/password pass through our server… Yikes*", "*cool — but dangerous*") is aimed at the **right** column.

---

## What people are actually doing (the real accounts)

### 1. JC Merlo (@itsjcmerlo) — small-account autonomous momentum bot *(local X thread)*
- Funded the account up to a "**solid $100**" ("STEP 4 add some gasoline — I added $35").
- Bot targets **1% moves**, but he's already self-correcting: *"it going for 1% but realistically you don't
  want it looking for that because you can't be jumping around comfortably"* — i.e. a 1% trigger fires too
  often / whipsaws.
- **Full autonomy via Windows Task Scheduler** during market hours ("while your computer is awake … during
  trading hours") — STEP 6.
- Posted **evidence of a live trade: BOUGHT $TSLA** (~$423.70) — STEP 7.
- **Takeaway for us**: this is the archetype — a roll-your-own scheduled momentum loop on a ~$100 account.
  His own caveat (1% is too tight) is a free lesson: pick a threshold wide enough to avoid death-by-spread.

### 2. The Trayd builder (team-trayd) — "I built an MCP server to trade Robinhood through Claude Code"
- Built a **natural-language trading** MCP: *"Instead of the app, I just type"* — "What's my portfolio
  worth?", "Buy 10 shares of AAPL", "Place a limit order for TSLA at $400".
- Architecture: MCP server on AWS ECS, OAuth via Clerk, **tokens in-memory only**, 2FA phone approval.
- **Caveat (important)**: this is a **third-party** server — auth passes through them because (at the time)
  RH had no OAuth. This is exactly the pattern the official MCP now replaces. We take the *UX idea*
  (conversational trading) but **not** the credential model.

### 3. Hacker News practitioners — the skeptics' chorus
- Reaction to the DIY approach: appreciation for the UX, real alarm at the credential flow. Representative:
  *"It is legitimately hard to tell whether this is innocent satire or actual malware."*
- Net signal: **the community trusts isolated/official auth, distrusts password-passing.** Validates our
  choice to stay on the official MCP.

### 4. Pattern-level usage seen across sources
- **Natural-language scheduled rules**, no code: *"Buy $100 of ROAR every time the price decreases 2% or more
  in 1 day."*
- **Ladder limit orders**: "5 ladder limit orders … 50+ clicks and 5 minutes" collapsed to "one sentence in
  10 seconds" — automation of tedium, not just signal-chasing.
- **Theme baskets / rebalancing**: "execute predefined investment themes (e.g. 'AI stocks basket')",
  concentration-risk and sector-exposure analysis, rebalancing toward a goal.

---

## The official safety model (what our account gives us)

From Robinhood docs, TechCrunch, and the ChatForest builder's-log:
- **Structural isolation**: the agent only touches capital you deposit into the agentic account; it gets
  **read-only** access to everything else (balances, positions, history) and can **only place trades in the
  agentic account**. "Blast radius bounded by design, not policy."
- **Kill switch**: one-tap disconnect of the agent, anytime.
- **Trade preview** + **per-trade push notifications**: optional **manual-approval toggle** flips to
  human-in-the-loop per transaction without disconnecting.
- **Up to 10** self-directed individual accounts; primary must be in good standing; desktop-only to set up.
- **Equities only** in beta; options/crypto/futures/event-contracts roadmapped.
- **You are fully responsible.** "AI agents can make errors, misinterpret instructions, act on incomplete or
  outdated information." Robinhood "does not supervise, control, or guarantee AI agent performance."

These map almost 1:1 onto the guards already in our `CLAUDE.md` (review-before-place, confirm-first,
read-tools-free/write-tools-gated). We verified the full flow end-to-end on 2026-06-03 (placed + cancelled a
resting F limit order in account `••••4606`).

## The skeptic's column (so we go in clear-eyed)
- **Motley Fool**: the AI launch is unlikely to move HOOD because the story is *fundamentals* — revenue
  growth decelerating (100% → 27% → 15% YoY), member-growth stalling, crypto-tethered. AI trading "comes
  with its own set of risks" and **amplifies** RH's already-volatile product mix.
- **Execution reality at micro-capital**: 1% of a $64–$100 account is **$0.64–$1.00** per winning trade.
  The bid/ask spread on our test (F: bid $15.71 / ask $15.95 ≈ **1.5%**) is *wider than the target edge*.
  At this size, momentum scalping is **negative expectancy after costs** — it's a learning harness, not a
  money-maker, until funded materially higher.

---

## What we extract for our setup

1. **Stay on the official MCP.** Safer auth, isolated account, kill switch. Skip every third-party server.
2. **Conversational + scheduled is the winning pattern.** Natural-language intent ("buy $X of Y when Z"),
   executed on a timer.
3. **Don't chase 1%.** JC Merlo's own correction + our spread math say the trigger must clear the spread with
   margin. Wider thresholds, fewer trades.
4. **Autonomous, with code-level guardrails** *(owner decision)*. No per-trade human approval. The "written
   autonomy boundary" is exactly the hard caps + daily circuit breaker + logging + kill switch in
   `CLAUDE.md` and `.env` — that's what replaces human sign-off.
5. **Paper until funded.** The account is being funded with a few thousand dollars; until it lands we run in
   `paper` to validate the engine, then flip to `live`. (Micro-capital momentum has no edge after costs — the
   funding is what makes it viable.)

---

## Proposed setup (v0) — see `strategies/momentum-v0-plan.md`

A small-capital momentum harness on the official MCP:
- **Mode**: `paper` first (sim against live quotes), `live` only on explicit opt-in.
- **Universe**: a handful of liquid, low-priced, fractional-eligible names (so $-based orders work).
- **Signal**: intraday momentum with a threshold **wider than the spread** (e.g. ≥ ~1.5–2% with confirmation),
  not 1%.
- **Sizing**: dollar-based, capped by `.env` risk limits (`MAX_POSITION_USD`, `MAX_PER_TRADE_LOSS_USD`).
- **Execution**: `review_equity_order` → (paper: log / live: confirm) → `place_equity_order`; marketable
  limits over market orders for price protection.
- **Scheduling**: Claude Code `/loop` or `/schedule` (cron) during regular hours — our equivalent of JC
  Merlo's Task Scheduler, but with our confirm-first guard and a kill switch (just disconnect the MCP).
- **Logging**: every decision + fill to `data/` for honest P&L review.

---

## Sources

1. [Agentic Trading overview — Robinhood (official docs)](https://robinhood.com/us/en/support/articles/agentic-trading-overview/)
2. [Robinhood now lets your AI agents trade stocks — TechCrunch](https://techcrunch.com/2026/05/27/robinhood-now-lets-your-ai-agents-trade-stocks/)
3. [Robinhood Launches Agentic AI for Stock Trading. Here's Why It Might Not Move the Stock — The Motley Fool](https://www.fool.com/investing/2026/06/02/robinhood-agentic-artificial-intelligence-stock/)
4. [Robinhood Opens Finance to AI Agents via MCP — ChatForest (builder's log)](https://chatforest.com/builders-log/robinhood-agentic-trading-mcp-finance-agents/)
5. [I Built an MCP Server to Trade Robinhood Through Claude Code — DEV Community (Trayd builder)](https://dev.to/teamtrayd_d74d7eeeed4003/i-built-an-mcp-server-to-trade-robinhood-through-claude-code-34ld)
6. [Show HN: I built an MCP server to trade Robinhood through Claude Code — Hacker News (practitioner discussion)](https://news.ycombinator.com/item?id=46429338)
7. JC Merlo (@itsjcmerlo) — X/Twitter thread on a small-account autonomous momentum bot *(local: `.claude/X post for agentic momentum trading`)*

*Supporting/edge sources surfaced but not primary: [Fortune](https://fortune.com/2026/05/27/robinhood-ai-agents/),
[Robinhood newsroom](https://robinhood.com/us/en/newsroom/robinhood-is-now-open-to-agents/),
[trayd-mcp GitHub](https://github.com/trayders/trayd-mcp).*
