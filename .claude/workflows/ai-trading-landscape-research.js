export const meta = {
  name: 'ai-trading-landscape-research',
  description: 'Sweep the last 3-6mo of AI/agentic trading techniques, adversarially verify them, synthesize a grounded plan to update our Robinhood momentum engine + backtest designs',
  phases: [
    { title: 'Sweep', detail: '10 parallel web-research angles (academic, GitHub, Composer, Reddit, X, news, sentiment, backtest-method, momentum, execution)' },
    { title: 'Curate', detail: 'dedup + rank all findings into a master technique list' },
    { title: 'Verify', detail: 'deep-read primary sources + adversarial credibility check per technique' },
    { title: 'Synthesize', detail: 'draft report -> completeness critic -> final grounded plan' },
  ],
}

// ---------- Our system context (so synthesis is grounded, not generic) ----------
const OUR_CONTEXT = `
WE ARE: an autonomous agentic trading engine on the OFFICIAL Robinhood Agentic MCP (isolated account, equities-only beta).
Stack: discovery (keyless Nasdaq top-gainers screen) -> tick_context.py (deterministic screen+gate) -> decide.py (Stage-2 LLM due-diligence commit, Sonnet+web) -> executor (paper: apply_decision.py simulating marketable-limit fills+slippage; live: live_execute.py via a tightly-scoped MCP relay agent rh_mcp.py). Runs on a ~5-min cron tick during market hours. Risk guardrails all in .env, enforced in code: per-name 10% cap, 80% exposure, 4% stop / 12% TP, 1%/trade loss budget, 5% daily circuit breaker, EOD flatten, scale-out ladder, cooldown.
DATA: keyless daily OHLCV from Cboe's CDN (split-adjusted back to 2004), cached under data/backtest/history/. Owner WILL NOT use a paid API key. Yahoo returns 429, Stooq is gated.
WHAT WE ALREADY PROVED (our own backtests, scripts/backtest_signal.py + backtest_xsection.py):
  (1) The live engine's INTRADAY absolute-pop signal (buy a name up >=3% on the day, >=3% vs SPY) has NO edge at the 1-day horizon it actually trades -- it is ANTI-PREDICTIVE (short-term reversal). A modest momentum edge only appears at 5-10 day holds the engine is architecturally built to AVOID (it EOD-flattens). Being MORE selective makes 1-day WORSE. Tuning slippage/stops cannot fix this; the ENTRY is the problem.
  (2) Cross-sectional 12-1 momentum (rank universe by trailing 3-6mo return skipping last month, buy top-5, monthly rotation, overnight holds) DOES show a statistically defensible edge: ~+12%/yr over equal-weight, t~3.0, but 61% max drawdown. HEAVILY survivorship-biased (fixed universe of today's 60 large-caps). The gating next step is a POINT-IN-TIME survivorship-free universe; if the edge survives that it is real, if it collapses it was the bias.
KEY TENSION: our live engine is intraday (the dead horizon); our backtest edge is multi-day swing (the live engine avoids it). Any recommendation must address this mismatch.
`.trim()

// ---------- Schemas ----------
const FINDING = {
  type: 'object',
  additionalProperties: false,
  properties: {
    technique: { type: 'string', description: 'name of the algo/approach/tool/framework' },
    category: { type: 'string', description: 'one of: llm-agent | multi-agent | deep-RL | factor-momentum | sentiment-news | no-code-retail | execution-microstructure | data-source | backtest-method | framework-library | other' },
    summary: { type: 'string', description: '2-4 sentences: what it is and the core idea' },
    whos_using_it: { type: 'string', description: 'who is doing/publishing this (lab, vendor, retail community, named practitioner)' },
    evidence_quality: { type: 'string', enum: ['peer-reviewed', 'strong-empirical', 'practitioner-anecdote', 'vendor-claim', 'hype-or-unverified'] },
    recency: { type: 'string', description: 'publication/post date or window. MUST be within ~Dec 2025 - Jun 2026.' },
    relevance_to_us: { type: 'string', enum: ['high', 'medium', 'low'] },
    source_urls: { type: 'array', items: { type: 'string' } },
    notable_quote: { type: 'string' },
  },
  required: ['technique', 'category', 'summary', 'evidence_quality', 'recency', 'relevance_to_us', 'source_urls'],
}
const SWEEP_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    angle: { type: 'string' },
    findings: { type: 'array', items: FINDING },
    dead_ends: { type: 'array', items: { type: 'string' }, description: 'things that looked promising but are hype/scam/stale' },
    notes: { type: 'string' },
  },
  required: ['angle', 'findings'],
}
const CURATE_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    techniques: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        properties: {
          name: { type: 'string' },
          one_liner: { type: 'string' },
          category: { type: 'string' },
          why_relevant: { type: 'string', description: 'why it matters for OUR momentum/agentic Robinhood setup specifically' },
          best_sources: { type: 'array', items: { type: 'string' } },
          priority: { type: 'string', enum: ['high', 'medium', 'low'] },
        },
        required: ['name', 'one_liner', 'why_relevant', 'best_sources', 'priority'],
      },
    },
    themes: { type: 'array', items: { type: 'string' }, description: 'cross-cutting themes you noticed across all angles' },
  },
  required: ['techniques'],
}
const VERDICT_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    name: { type: 'string' },
    verdict: { type: 'string', enum: ['credible-edge', 'promising-unproven', 'overfit-risk', 'hype-or-marketing', 'scam-or-snakeoil', 'not-applicable'] },
    reasoning: { type: 'string' },
    pitfalls: { type: 'array', items: { type: 'string' }, description: 'survivorship, lookahead/leakage, overfitting, transaction-cost-blindness, regime-dependence, data-snooping, etc.' },
    applicability: { type: 'string', description: 'concretely how (or whether) it maps to our intraday/swing Robinhood momentum engine' },
    recommended_action: { type: 'string', enum: ['adopt', 'prototype', 'backtest-first', 'monitor', 'ignore'] },
    backtest_design: { type: 'string', description: 'if backtest-first/prototype: a concrete test we could run with keyless Cboe daily OHLCV (or what extra data is needed)' },
    sources_read: { type: 'array', items: { type: 'string' } },
  },
  required: ['name', 'verdict', 'reasoning', 'recommended_action', 'applicability'],
}
const CRITIQUE_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    missing_angles: { type: 'array', items: { type: 'string' } },
    unsupported_claims: { type: 'array', items: { type: 'string' } },
    gaps: { type: 'array', items: { type: 'string' } },
    strengthen: { type: 'array', items: { type: 'string' } },
  },
  required: ['gaps'],
}
const REPORT_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    report_markdown: { type: 'string', description: 'the FULL final report as GitHub-flavored markdown, ready to write to a file' },
    executive_summary: { type: 'string' },
    top_workflow_updates: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        properties: {
          change: { type: 'string' },
          rationale: { type: 'string' },
          files_touched: { type: 'string' },
          effort: { type: 'string', enum: ['S', 'M', 'L'] },
        },
        required: ['change', 'rationale', 'effort'],
      },
    },
    top_backtest_plans: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        properties: {
          test: { type: 'string' },
          data_needed: { type: 'string' },
          expected_signal: { type: 'string' },
        },
        required: ['test', 'data_needed'],
      },
    },
  },
  required: ['report_markdown', 'executive_summary', 'top_workflow_updates', 'top_backtest_plans'],
}

// ---------- Phase 1: Sweep ----------
const SWEEP_HEADER = `You are a skeptical quant research scout. Today is 2026-06-04. STRICT TIME WINDOW: only count material published/posted in the last ~3-6 months (roughly Dec 2025 - Jun 2026). Ignore anything older unless it is THE canonical reference a recent piece builds on (note it as such).

Use WebSearch and WebFetch aggressively: run multiple distinct queries, open the most promising results, and READ them (don't trust snippets). Capture exact source URLs and dates. Grade evidence honestly with the evidence_quality enum -- most "AI trading" content online is marketing or survivorship-biased backtests; say so. Flag dead_ends (hype/scams/stale).

For each real finding, fill the schema. relevance_to_us = how much it could change OUR system (context below). Prefer specific, implementable techniques over vague trends.

OUR SYSTEM CONTEXT:
${OUR_CONTEXT}

YOUR ASSIGNED ANGLE:`

const ANGLES = [
  { key: 'academic', brief: `ACADEMIC / arXiv (q-fin, cs.LG) Dec 2025-Jun 2026. Hunt the newest papers on: LLM agents for trading (e.g. TradingAgents, FinMem, FINMEM, FinAgent, multi-agent debate trading), LLM-driven alpha from news/filings, time-series FOUNDATION MODELS for finance (TimesFM, Chronos, Moirai, Time-MoE and any 2026 successors applied to returns), deep-RL portfolio/execution (FinRL 2026 updates), and any work specifically on CROSS-SECTIONAL MOMENTUM refinements, regime filters, or survivorship-bias-robust evaluation. Note reported edge AND whether costs/survivorship were handled.` },
  { key: 'github', brief: `OPEN-SOURCE / GitHub trending in the last 6 months. Find actively-developed (recent commits Dec2025+) repos for: agentic/LLM trading frameworks, MCP-based trading servers, FinRL / Qlib / FinGPT ecosystem updates, retail algo bots, and backtesting engines (vectorbt/VectorBT PRO, nautilus_trader, backtesting.py, lean/QuantConnect). For each: stars/activity, what it does, license, and whether it's a usable component for us vs a toy.` },
  { key: 'composer', brief: `COMPOSER.TRADE + no-code / retail-quant platforms (Composer symphonies, Surmount, Tickeron, Trade Ideas Holly AI, Numerai, QuantConnect community, Capitalise.ai, Anthropic/OpenAI-powered retail tools). What strategies are people actually running and sharing in the last 6 months? Composer's "AI" strategy builder, popular symphonies, momentum/sector-rotation templates. Extract concrete, copyable strategy STRUCTURES (rules, rebalance cadence, universe) -- not marketing.` },
  { key: 'reddit', brief: `REDDIT practitioner sentiment, last 6 months: r/algotrading, r/quant, r/LocalLLaMA, r/Daytrading, r/Trading, r/MachineLearning. What's the current consensus on LLM/agentic trading -- what's reported to WORK, what's dismissed as snake oil, what tooling people actually use. Capture honest failure post-mortems and the recurring "this is why your backtest lies" wisdom (survivorship, lookahead, overfitting, costs). Link the specific threads.` },
  { key: 'twitter', brief: `TWITTER/X (fintwit / quant / AI-agent builders), last 6 months. Agentic trading bots (incl. the Robinhood Agentic MCP scene specifically), LLM-alpha claims, prompt-driven trading, people posting live agentic-trading P&L. Separate credible practitioners from grifters. Capture handles, post dates, and any concrete method described.` },
  { key: 'journalism', brief: `INDUSTRY / JOURNALISM / product launches, Dec 2025-Jun 2026. New AI-trading products & brokerage agentic features (Robinhood Agentic MCP and any competitor responses -- Schwab, IBKR, Webull, Public, etc.), hedge-fund / prop-shop LLM adoption news, regulatory commentary on agentic trading. TechCrunch, Bloomberg, Reuters, FT, etc. What is the state of the agentic-trading market right now.` },
  { key: 'sentiment-news', brief: `LLM SENTIMENT / NEWS / ALT-DATA alpha, last 6 months. Latest on using LLMs to trade news, earnings calls, SEC filings, social sentiment; reported edge, decay, and the "everyone has the same LLM read" crowding problem. Concrete pipelines (news -> embedding/score -> signal), and any free/cheap news data sources. Especially anything intraday/event-driven that could replace our dead intraday absolute-pop entry with a CATALYST-driven entry.` },
  { key: 'backtest-method', brief: `BACKTESTING METHODOLOGY + FREE DATA, last 6 months but include canonical methods recent pieces cite. Walk-forward, purged/embargoed cross-validation (Lopez de Prado), combinatorial CV, deflated Sharpe, multiple-testing correction, event-driven vs vectorized backtesting, realistic transaction-cost/slippage modeling. CRITICALLY: SURVIVORSHIP-FREE / point-in-time universe data that is FREE or keyless (delisted tickers, historical index constituents) -- this is our #1 blocker. Name specific datasets/sources and whether they need a key.` },
  { key: 'momentum-factor', brief: `MOMENTUM & FACTOR research refinements, last 6 months. Anything new on cross-sectional momentum (our live edge candidate): residual/idiosyncratic momentum, time-series vs cross-sectional, momentum crash protection / volatility scaling / regime gating, "factor momentum", trend-following on equities, and 12-1 construction variants. We need to know how to make our top-5 monthly-rotation momentum survive out-of-sample and tame the 61% drawdown.` },
  { key: 'execution', brief: `EXECUTION & MICROSTRUCTURE for small/retail agentic accounts, last 6 months. Marketable-limit vs market, slippage at small size, why intraday scalping loses to costs, optimal rebalance frequency, tax/PDT constraints for sub-$25k accounts, and how agentic bots should schedule orders. Anything on order types available via brokerage agentic APIs. Validate or refute our 'wider-than-spread threshold, fewer trades' design.` },
]

phase('Sweep')
log(`Sweeping ${ANGLES.length} research angles across the last 3-6 months...`)
const sweeps = (await parallel(ANGLES.map((a) => () =>
  agent(`${SWEEP_HEADER}\n${a.brief}`, { label: `sweep:${a.key}`, phase: 'Sweep', schema: SWEEP_SCHEMA, agentType: 'general-purpose' })
))).filter(Boolean)

const allFindings = sweeps.flatMap((s) => (s.findings || []).map((f) => ({ ...f, angle: s.angle })))
const allDeadEnds = sweeps.flatMap((s) => (s.dead_ends || []))
log(`Sweep done: ${allFindings.length} findings, ${allDeadEnds.length} dead-ends across ${sweeps.length} angles.`)

// ---------- Phase 2: Curate (barrier: needs ALL findings to dedup+rank) ----------
phase('Curate')
const curated = await agent(
  `You are the research lead. Below are ${allFindings.length} raw findings from ${sweeps.length} parallel scouts on the latest (Dec 2025-Jun 2026) AI/agentic trading techniques.

DEDUPLICATE near-identical techniques into one entry (merge their sources). RANK by genuine implementability + relevance to OUR system. DROP pure hype/marketing/scams (but keep them in mind for the report's "what to ignore" section). Produce a master list of the most important DISTINCT techniques/approaches/tools (aim for the ~15-22 that matter), each with why it's relevant to US specifically and its best 1-3 source URLs. Also list cross-cutting themes.

OUR SYSTEM CONTEXT:
${OUR_CONTEXT}

RAW FINDINGS (JSON):
${JSON.stringify(allFindings)}

KNOWN DEAD-ENDS (JSON):
${JSON.stringify(allDeadEnds)}`,
  { label: 'curate-rank', phase: 'Curate', schema: CURATE_SCHEMA }
)

const toVerify = (curated.techniques || [])
  .filter((t) => t.priority === 'high' || t.priority === 'medium')
  .slice(0, 18)
log(`Curated ${curated.techniques?.length || 0} techniques; deep-verifying top ${toVerify.length}.`)

// ---------- Phase 3: Verify (pipeline: deep-read -> adversarial credibility check) ----------
phase('Verify')
const verdicts = (await pipeline(
  toVerify,
  // stage 1: deep-read the primary sources
  (t) => agent(
    `Deep-read the PRIMARY sources for this technique and report what they ACTUALLY say (methods, reported results, caveats, dates). Use WebFetch on each source URL and WebSearch for the original paper/repo/thread if a link is dead. Be concrete about numbers and whether costs/survivorship/lookahead were handled. If you cannot verify it from primary sources, say so explicitly.

TECHNIQUE: ${t.name}
ONE-LINER: ${t.one_liner}
WHY WE CARE: ${t.why_relevant}
SOURCES: ${JSON.stringify(t.best_sources)}`,
    { label: `read:${(t.name || '').slice(0, 28)}`, phase: 'Verify', agentType: 'general-purpose' }
  ),
  // stage 2: adversarial credibility verdict, grounded in our system
  (deepNotes, t) => agent(
    `You are an adversarial quant skeptic. Default to disbelief; an AI-trading claim is guilty of survivorship/overfitting/cost-blindness until proven innocent. Given the deep-read notes below, render a verdict and -- crucially -- judge APPLICABILITY to OUR system and recommend an action. If recommending backtest-first or prototype, design a CONCRETE test runnable on our keyless Cboe daily OHLCV (or name the extra data needed).

OUR SYSTEM CONTEXT:
${OUR_CONTEXT}

TECHNIQUE: ${t.name} (${t.category || '?'})
WHY WE FLAGGED IT: ${t.why_relevant}

DEEP-READ NOTES:
${deepNotes}`,
    { label: `verify:${(t.name || '').slice(0, 26)}`, phase: 'Verify', schema: VERDICT_SCHEMA }
  )
)).filter(Boolean)

const keep = verdicts.filter((v) => v && !['scam-or-snakeoil', 'not-applicable'].includes(v.verdict))
log(`Verified ${verdicts.length} techniques; ${keep.length} survived the skeptic pass.`)

// ---------- Phase 4: Synthesize (draft -> critic -> final) ----------
phase('Synthesize')
const highRelFindings = allFindings.filter((f) => f.relevance_to_us === 'high')

const synthInput = `
VERIFIED TECHNIQUES (with skeptic verdicts, JSON):
${JSON.stringify(verdicts)}

CURATED THEMES:
${JSON.stringify(curated.themes || [])}

HIGH-RELEVANCE RAW FINDINGS (JSON):
${JSON.stringify(highRelFindings)}

DEAD-ENDS / WHAT-TO-IGNORE (JSON):
${JSON.stringify(allDeadEnds)}
`.trim()

const draft = await agent(
  `You are the head of quant research writing an internal memo for the owner of OUR system. Write a thorough, skeptical, ACTIONABLE markdown report titled "AI / Agentic Trading Landscape -- 2026 H1 (Dec 2025 - Jun 2026)".

Structure:
1. TL;DR (5-8 bullets: what's genuinely new and worth our time vs what's noise).
2. The landscape, grouped by category (LLM/multi-agent, deep-RL, factor/momentum, sentiment/news/catalyst, no-code/Composer, frameworks/data, backtest methodology). For each technique: what it is, who's doing it, evidence quality, and the skeptic verdict. Cite source URLs inline.
3. "What to ignore" -- the hype/scam patterns we saw.
4. **Plan to update OUR workflow** -- this is the point. Concrete, mapped to our actual files (tick_context.py, decide.py, discover.py, apply_decision.py, live_execute.py, backtest_*.py, .env). Directly confront our core tension: live engine is intraday (the DEAD horizon per our own backtest) while our only proven edge is multi-day cross-sectional momentum (which the live engine avoids). Recommend whether to pivot the live engine toward swing/overnight momentum, add a catalyst-driven intraday entry, run a parallel swing sleeve, etc. Be specific and ordered by impact.
5. **Backtest plans** -- concrete tests, prioritizing the survivorship-free-universe gate (our #1 blocker) and using our existing harnesses. Note which need data we can fetch keyless vs need new sources found in the sweep.
6. Sources.

Ground EVERYTHING in our context; do not give generic advice. Prefer specifics with file names and parameter values.

OUR SYSTEM CONTEXT:
${OUR_CONTEXT}

RESEARCH INPUT:
${synthInput}`,
  { label: 'draft-report', phase: 'Synthesize' }
)

const critique = await agent(
  `You are a completeness critic. Review this draft research memo for: missing recent (Dec2025-Jun2026) angles, unsupported/hand-wavy claims, gaps in the "update our workflow" plan (is it concrete and mapped to real files?), and weak backtest designs. Be specific and harsh.

OUR SYSTEM CONTEXT:
${OUR_CONTEXT}

DRAFT:
${draft}`,
  { label: 'completeness-critic', phase: 'Synthesize', schema: CRITIQUE_SCHEMA }
)

const final = await agent(
  `Revise the memo to fix every gap the critic raised, WITHOUT inventing facts (if a claim can't be supported, soften or cut it). Keep it concrete and mapped to our files. Then emit the final structured output: the full report markdown, an executive summary, the top workflow updates (with files_touched + effort S/M/L), and the top backtest plans.

OUR SYSTEM CONTEXT:
${OUR_CONTEXT}

DRAFT:
${draft}

CRITIC FINDINGS (JSON):
${JSON.stringify(critique)}`,
  { label: 'finalize-report', phase: 'Synthesize', schema: REPORT_SCHEMA }
)

return {
  report: final,
  stats: {
    angles: sweeps.length,
    findings: allFindings.length,
    curated: curated.techniques?.length || 0,
    verified: verdicts.length,
    survived: keep.length,
  },
}