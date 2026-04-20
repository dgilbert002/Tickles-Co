#!/bin/bash
set +e
LOG=/root/rubicon-deploy.log
log() { echo "$(date -u +%FT%TZ) [D3] $*" | tee -a "$LOG"; }
source /root/rubicon.env
PC=http://127.0.0.1:3100

ISSUES=$(curl -sS "$PC/api/companies/$COMPANY_ID/issues")
RUB1=$(echo "$ISSUES" | python3 -c "import json,sys;d=json.load(sys.stdin);print([i['id'] for i in d if i['identifier']=='RUB-1'][0])")
RUB2=$(echo "$ISSUES" | python3 -c "import json,sys;d=json.load(sys.stdin);print([i['id'] for i in d if i['identifier']=='RUB-2'][0])")

log "wakeup surgeon v1"
curl -sS -X POST "$PC/api/agents/$SURG_ID/wakeup" \
  -H "Content-Type: application/json" \
  -d "{\"source\":\"assignment\",\"triggerDetail\":\"manual\",\"reason\":\"First paper-trade cycle RUB-1\",\"payload\":{\"issueId\":\"$RUB1\"}}" | tee -a "$LOG"
echo "" | tee -a "$LOG"

log "wakeup surgeon v2"
curl -sS -X POST "$PC/api/agents/$SURG2_ID/wakeup" \
  -H "Content-Type: application/json" \
  -d "{\"source\":\"assignment\",\"triggerDetail\":\"manual\",\"reason\":\"First paper-trade cycle RUB-2\",\"payload\":{\"issueId\":\"$RUB2\"}}" | tee -a "$LOG"
echo "" | tee -a "$LOG"

sleep 10
log "post-wake state"
curl -sS "$PC/api/companies/$COMPANY_ID/issues" | python3 -c "
import json,sys
for i in json.load(sys.stdin):
    print(i['identifier'], i['status'], 'assignee=', i.get('assigneeAgentId'), 'run=', i.get('executionRunId'), 'lastAct=', i.get('lastActivityAt'))
" | tee -a "$LOG"

log "agent statuses"
for id in "$SURG_ID" "$SURG2_ID"; do
  curl -sS "$PC/api/agents/$id" | python3 -c "
import json,sys
a=json.load(sys.stdin)
print(a['name'], 'status=', a['status'], 'last_hb=', a.get('lastHeartbeatAt'))
" | tee -a "$LOG"
done
