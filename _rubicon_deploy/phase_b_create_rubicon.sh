#!/bin/bash
# Phase B: create Rubicon company + CEO + Surgeon + Surgeon2 via Paperclip HTTP API.
set +e
LOG=/root/rubicon-deploy.log
log() { echo "$(date -u +%FT%TZ) [B] $*" | tee -a "$LOG"; }

PC_API="http://127.0.0.1:3100"

# --- B1: create company 'Rubicon' ---
log "B1: POST /api/companies -> Rubicon"
CREATE_RESP=$(curl -sS -X POST "$PC_API/api/companies" \
  -H "Content-Type: application/json" \
  -d '{"name":"Rubicon","description":"Autonomous crypto trading desk. Paper-first. Surgeon + Surgeon2.","budgetMonthlyCents":2500}')
echo "$CREATE_RESP" | tee -a "$LOG"
COMPANY_ID=$(echo "$CREATE_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))")
if [ -z "$COMPANY_ID" ]; then
  log "FAIL: company not created"
  exit 1
fi
log "B1 OK: companyId=$COMPANY_ID"

# --- B2: disable board-approval for new agents ---
log "B2: PATCH companies/$COMPANY_ID requireBoardApprovalForNewAgents=false"
curl -sS -X PATCH "$PC_API/api/companies/$COMPANY_ID" \
  -H "Content-Type: application/json" \
  -d '{"requireBoardApprovalForNewAgents":false}' | tee -a "$LOG"
echo "" | tee -a "$LOG"

# --- B3a: hire CEO ---
log "B3a: hire CEO (rubicon_ceo)"
CEO_RESP=$(curl -sS -X POST "$PC_API/api/companies/$COMPANY_ID/agent-hires" \
  -H "Content-Type: application/json" \
  -d '{
    "name":"rubicon_ceo",
    "role":"ceo",
    "title":"Rubicon CEO",
    "capabilities":"Run the Rubicon crypto trading desk. Hire/fire traders. Paper-trading only until proven. Respect the $25/mo budget.",
    "adapterType":"openclaw_gateway",
    "budgetMonthlyCents":500,
    "permissions":{"canCreateAgents":true}
  }')
echo "$CEO_RESP" | tee -a "$LOG"
CEO_ID=$(echo "$CEO_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
log "B3a done: ceoId=$CEO_ID"

# --- B3b: hire Surgeon (Twilly-faithful flat-file version) ---
log "B3b: hire Surgeon (Twilly flat-file)"
SURG_RESP=$(curl -sS -X POST "$PC_API/api/companies/$COMPANY_ID/agent-hires" \
  -H "Content-Type: application/json" \
  -d "{
    \"name\":\"rubicon_surgeon\",
    \"role\":\"general\",
    \"title\":\"The Surgeon (v1, flat-file)\",
    \"reportsTo\":\"$CEO_ID\",
    \"capabilities\":\"Twilly-spec Surgeon. Reads MARKET_STATE.json + MARKET_INDICATORS.json and appends decisions to TRADE_STATE.md + TRADE_LOG.md. Paper trading only.\",
    \"adapterType\":\"openclaw_gateway\",
    \"budgetMonthlyCents\":1000
  }")
echo "$SURG_RESP" | tee -a "$LOG"
SURG_ID=$(echo "$SURG_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
log "B3b done: surgeonId=$SURG_ID"

# --- B3c: hire Surgeon2 (Tickles MCP adaptation) ---
log "B3c: hire Surgeon2 (MCP adaptation)"
SURG2_RESP=$(curl -sS -X POST "$PC_API/api/companies/$COMPANY_ID/agent-hires" \
  -H "Content-Type: application/json" \
  -d "{
    \"name\":\"rubicon_surgeon2\",
    \"role\":\"general\",
    \"title\":\"The Surgeon (v2, MCP-backed)\",
    \"reportsTo\":\"$CEO_ID\",
    \"capabilities\":\"Surgeon adaptation using Tickles MCP + Postgres. Reads candles, derivatives_snapshots, writes orders/fills, logs decisions to agent_decisions. Paper trading only.\",
    \"adapterType\":\"openclaw_gateway\",
    \"budgetMonthlyCents\":1000
  }")
echo "$SURG2_RESP" | tee -a "$LOG"
SURG2_ID=$(echo "$SURG2_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
log "B3c done: surgeon2Id=$SURG2_ID"

# --- persist ids ---
cat > /root/rubicon.env <<EOF
COMPANY_ID=$COMPANY_ID
CEO_ID=$CEO_ID
SURG_ID=$SURG_ID
SURG2_ID=$SURG2_ID
EOF
log "IDs written to /root/rubicon.env"
cat /root/rubicon.env | tee -a "$LOG"
