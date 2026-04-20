#!/bin/bash
# Re-hire Surgeon + Surgeon2 using correct CEO id parsing.
set +e
LOG=/root/rubicon-deploy.log
log() { echo "$(date -u +%FT%TZ) [B-fix] $*" | tee -a "$LOG"; }

PC_API="http://127.0.0.1:3100"
COMPANY_ID="18440bb4-0c4b-4c15-8e66-c318e045f653"

# re-discover CEO id (no double-creation)
log "finding existing CEO id for company $COMPANY_ID"
CEO_LIST=$(curl -sS "$PC_API/api/companies/$COMPANY_ID/agents")
echo "$CEO_LIST" | python3 -m json.tool 2>/dev/null | head -40 | tee -a "$LOG"
CEO_ID=$(echo "$CEO_LIST" | python3 -c "
import json, sys
d = json.load(sys.stdin)
agents = d if isinstance(d, list) else d.get('agents', [])
for a in agents:
    if a.get('role') == 'ceo':
        print(a['id']); break
")
log "CEO_ID=$CEO_ID"
if [ -z "$CEO_ID" ]; then
  log "FAIL: CEO not found"
  exit 1
fi

# hire Surgeon
log "hire Surgeon"
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
SURG_ID=$(echo "$SURG_RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print((d.get('agent') or {}).get('id',''))")
log "surgeonId=$SURG_ID"

# hire Surgeon2
log "hire Surgeon2"
SURG2_RESP=$(curl -sS -X POST "$PC_API/api/companies/$COMPANY_ID/agent-hires" \
  -H "Content-Type: application/json" \
  -d "{
    \"name\":\"rubicon_surgeon2\",
    \"role\":\"general\",
    \"title\":\"The Surgeon (v2, MCP-backed)\",
    \"reportsTo\":\"$CEO_ID\",
    \"capabilities\":\"Surgeon adaptation using Tickles MCP + Postgres. Reads candles/derivatives_snapshots, writes orders/fills, logs decisions to agent_decisions. Paper trading only.\",
    \"adapterType\":\"openclaw_gateway\",
    \"budgetMonthlyCents\":1000
  }")
echo "$SURG2_RESP" | tee -a "$LOG"
SURG2_ID=$(echo "$SURG2_RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print((d.get('agent') or {}).get('id',''))")
log "surgeon2Id=$SURG2_ID"

cat > /root/rubicon.env <<EOF
COMPANY_ID=$COMPANY_ID
CEO_ID=$CEO_ID
SURG_ID=$SURG_ID
SURG2_ID=$SURG2_ID
EOF
log "IDs:"
cat /root/rubicon.env | tee -a "$LOG"
