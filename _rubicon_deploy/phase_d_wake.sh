#!/bin/bash
# Phase D part 2: put issues into todo status, then wake both surgeons
# so openclaw starts processing them.
set +e
LOG=/root/rubicon-deploy.log
log() { echo "$(date -u +%FT%TZ) [D2] $*" | tee -a "$LOG"; }
source /root/rubicon.env
PC=http://127.0.0.1:3100

ISSUES=$(curl -sS "$PC/api/companies/$COMPANY_ID/issues")
RUB1=$(echo "$ISSUES" | python3 -c "import json,sys;d=json.load(sys.stdin);print([i['id'] for i in d if i['identifier']=='RUB-1'][0])")
RUB2=$(echo "$ISSUES" | python3 -c "import json,sys;d=json.load(sys.stdin);print([i['id'] for i in d if i['identifier']=='RUB-2'][0])")
log "RUB-1=$RUB1  RUB-2=$RUB2"

for iid in "$RUB1" "$RUB2"; do
  log "PATCH issue $iid status=todo"
  curl -sS -X PATCH "$PC/api/issues/$iid" -H "Content-Type: application/json" -d '{"status":"todo"}' | head -c 400 | tee -a "$LOG"
  echo "" | tee -a "$LOG"
done

log "find wake route"
grep -RIn "router\.post.*wake\|/wake\"" /root/paperclip/server/src/routes/agents.ts 2>/dev/null | head -10 | tee -a "$LOG"

# Try wake endpoints
log "wake surgeon v1"
curl -sS -X POST "$PC/api/agents/$SURG_ID/wake" -H "Content-Type: application/json" \
  -d "{\"source\":\"on_demand\",\"reason\":\"First paper-trade cycle assigned (RUB-1)\",\"payload\":{\"issueId\":\"$RUB1\"}}" | head -c 500 | tee -a "$LOG"
echo "" | tee -a "$LOG"

log "wake surgeon v2"
curl -sS -X POST "$PC/api/agents/$SURG2_ID/wake" -H "Content-Type: application/json" \
  -d "{\"source\":\"on_demand\",\"reason\":\"First paper-trade cycle assigned (RUB-2)\",\"payload\":{\"issueId\":\"$RUB2\"}}" | head -c 500 | tee -a "$LOG"
echo "" | tee -a "$LOG"

sleep 5
log "post-wake issues"
curl -sS "$PC/api/companies/$COMPANY_ID/issues" | python3 -c "
import json,sys
for i in json.load(sys.stdin):
    print(i['identifier'], i['status'], 'assignee=', i.get('assigneeAgentId'), 'run=', i.get('executionRunId'))
" | tee -a "$LOG"
