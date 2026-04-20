#!/bin/bash
set +e
LOG=/root/rubicon-deploy.log
log() { echo "$(date -u +%FT%TZ) [runs3] $*" | tee -a "$LOG"; }
source /root/rubicon.env
PC=http://127.0.0.1:3100

log "live runs company-scoped"
curl -sS "$PC/api/companies/$COMPANY_ID/live-runs?limit=5" | python3 -m json.tool 2>/dev/null | head -120 | tee -a "$LOG"

log "heartbeat runs company-scoped"
curl -sS "$PC/api/companies/$COMPANY_ID/heartbeat-runs?limit=5" | python3 -m json.tool 2>/dev/null | head -120 | tee -a "$LOG"

log "get issue runs RUB-1"
curl -sS "$PC/api/issues/88e44840-b1bf-4778-bb34-5618b000ee03/runs?limit=3" | python3 -m json.tool 2>/dev/null | head -120 | tee -a "$LOG"
curl -sS "$PC/api/issues/88e44840-b1bf-4778-bb34-5618b000ee03/live-runs?limit=3" | python3 -m json.tool 2>/dev/null | head -120 | tee -a "$LOG"

log "agent status + last activity"
curl -sS "$PC/api/agents/$SURG_ID" | python3 -m json.tool 2>/dev/null | head -80 | tee -a "$LOG"
