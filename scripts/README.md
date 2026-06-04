# scripts/

Headless, read-only jobs for the agentic-trading engine. None of these touch the Robinhood
account or place orders — they assess the market and log. Order execution lives in the (future)
engine, which is agent-driven via the MCP.

## `market_conditions.py` — market-regime checker
Pulls index ETFs (SPY/QQQ/IWM/DIA) + VIXY (VIX proxy) from Stooq's **keyless** CSV endpoint,
classifies the session's **posture / volatility / breadth**, prints a one-line summary, and
appends a structured record to `data/market_conditions.jsonl`. **Stdlib only** — nothing to
`pip install`. Self-accumulates SPY closes so a trend signal comes online after ~5 logged
sessions.

```bash
python3 scripts/market_conditions.py          # check + log + summary
python3 scripts/market_conditions.py --json   # full record as JSON
python3 scripts/market_conditions.py --quiet  # log only
```

Why public data, not the Robinhood MCP: the MCP authenticates through the interactive Claude
client and **may be absent in a headless/cron run**. Market-regime data is public, so this job
is fully self-contained and runs anywhere.

## `run_market_check.sh` — cron/launchd wrapper
Cron-safe wrapper: absolute paths, minimal-PATH tolerant, never fails the scheduler. Adds a
dated run log under `data/logs/` on top of the JSONL record.

```bash
scripts/run_market_check.sh
```

Override the interpreter if needed: `AGENTIC_PYTHON=/path/to/python3 scripts/run_market_check.sh`.

---

## Scheduling it headless (this machine's TZ is US/Eastern, so cron times = ET)

### Option A — cron (simplest)
```bash
crontab -e
```
Add (every 30 min, weekdays, ~market hours; the script self-labels off-hours runs, so the
edge ticks at :00/:30 around the open/close are harmless):
```cron
*/30 9-16 * * 1-5 /Users/mcosta/Documents/workrepos/agentic-trading/scripts/run_market_check.sh >/dev/null 2>&1
```
**macOS gotchas:**
- Grant **Full Disk Access** to `/usr/sbin/cron` (System Settings → Privacy & Security → Full
  Disk Access) or cron can't read files under `~/Documents`.
- The Mac must be **awake** during runs. Keep it awake on a schedule with `caffeinate`, or via
  `pmset`. Sleep = missed ticks.

### Option B — launchd (more robust on macOS)
Create `~/Library/LaunchAgents/com.agentic.marketcheck.plist` running `run_market_check.sh` on a
`StartCalendarInterval`, then `launchctl load` it. launchd survives reboots and is the native
scheduler; ask and I'll generate the plist.

### Verify it's running
```bash
tail -f data/logs/market_check_$(date +%F).log     # run log
tail -n 5 data/market_conditions.jsonl             # structured records
```

## Headless MCP — VERIFIED working (2026-06-04)
The trading engine needs the Robinhood MCP (account data + orders), and **headless access is
confirmed**. A non-interactive probe reused the stored OAuth token with no browser step:
```bash
claude -p 'Call get_accounts and report only the count' \
  --mcp-config /Users/mcosta/Documents/workrepos/agentic-trading/.mcp.json \
  --allowedTools 'mcp__robinhood-trading__get_accounts' \
  --dangerously-skip-permissions --output-format text
# -> {"ok": true, "num_accounts": 4}  (exit 0, ~15s, no interactive auth)
```
So the autonomous engine path is: **cron → `claude -p` (headless) in this repo → MCP tools**,
scoped by `--allowedTools` and the `.env` risk caps. For live order placement, the allow-list
expands to include `place_equity_order` / `cancel_equity_order`. The market-conditions cron above
stays independent (public data, no auth) as a robust regime feed the engine can read.
