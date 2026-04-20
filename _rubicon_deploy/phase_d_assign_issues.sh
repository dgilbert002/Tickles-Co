#!/bin/bash
# Phase D: create first paper-trade issue for Surgeon and Surgeon2.
set +e
LOG=/root/rubicon-deploy.log
log() { echo "$(date -u +%FT%TZ) [D] $*" | tee -a "$LOG"; }
source /root/rubicon.env
PC=http://127.0.0.1:3100

# Find the issue creation route.
log "issue routes"
grep -RIn "router\.post" /root/paperclip/server/src/routes/issues.ts 2>/dev/null | head -20 | tee -a "$LOG"

# Create issue for Surgeon (v1, flat-file)
log "create issue -> Surgeon v1"
SURG_ISSUE=$(curl -sS -X POST "$PC/api/companies/$COMPANY_ID/issues" \
  -H "Content-Type: application/json" \
  -d "{
    \"title\": \"First paper-trade cycle — Surgeon v1\",
    \"description\": \"You are THE SURGEON (v1, flat-file). Perform your ON EVERY SPAWN routine exactly per your SOUL.md at /root/.openclaw/agents/rubicon_surgeon/SOUL.md.\\n\\n1. Read /root/.openclaw/workspace/rubicon_surgeon/TRADE_STATE.md.\\n2. Read /root/.openclaw/workspace/rubicon_surgeon/MARKET_STATE.json and MARKET_INDICATORS.json.\\n3. Score BTCUSDT, ETHUSDT, SOLUSDT against your entry signals.\\n4. If a signal qualifies: open a paper position, size per your POSITION SIZING rules, update TRADE_STATE.md, append to TRADE_LOG.md using the TRADE LOG FORMAT. Do NOT call real execution; paper only.\\n5. If no signal qualifies, log your top 3 candidates with scores to TRADE_LOG.md and return.\\n\\nAt the end, post a Paperclip comment (<=600 chars) with: current balance, open positions count, any trade you took (or 'no trade'), and top 3 asset scores.\",
    \"priority\": \"high\",
    \"assigneeAgentId\": \"$SURG_ID\"
  }")
echo "$SURG_ISSUE" | python3 -m json.tool 2>/dev/null | head -20 | tee -a "$LOG"

# Create issue for Surgeon2 (MCP-backed)
log "create issue -> Surgeon v2"
SURG2_ISSUE=$(curl -sS -X POST "$PC/api/companies/$COMPANY_ID/issues" \
  -H "Content-Type: application/json" \
  -d "{
    \"title\": \"First paper-trade cycle — Surgeon v2 (MCP)\",
    \"description\": \"You are THE SURGEON v2 (MCP-backed). Follow your SOUL.md at /root/.openclaw/agents/rubicon_surgeon2/SOUL.md.\\n\\n1. MCP ping.\\n2. Call md.candles for BTCUSDT, ETHUSDT, SOLUSDT (1m, last 60). If md.candles is a stub, fall back to reading /root/.openclaw/workspace/rubicon_surgeon/MARKET_STATE.json as a proxy and note that you did so in your decision log.\\n3. Query derivatives_snapshots (via banker.snapshot or a direct SELECT if available) for latest funding rates per asset.\\n4. Score divergence + funding per Surgeon v1 rules.\\n5. If a signal qualifies: record a PAPER decision via memory.add (scope='agent', topic='trade_decision') with side, symbol, notional, leverage, stop_pct, tp_pcts. Do NOT call execution.submit live in this first run. Mark mode='paper-dry'.\\n6. Post a Paperclip comment (<=600 chars) with: latest candle ts, open positions count (banker.positions), chosen signal (or 'no trade'), top 3 scores.\\n\\nIf any MCP tool fails, note the failure and proceed with the next step. Never fabricate data.\",
    \"priority\": \"high\",
    \"assigneeAgentId\": \"$SURG2_ID\"
  }")
echo "$SURG2_ISSUE" | python3 -m json.tool 2>/dev/null | head -20 | tee -a "$LOG"

log "list issues in Rubicon"
curl -sS "$PC/api/companies/$COMPANY_ID/issues" | python3 -m json.tool 2>/dev/null | head -50 | tee -a "$LOG"
