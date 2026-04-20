#!/bin/bash
set +e
LOG=/root/rubicon-deploy.log
log() { echo "$(date -u +%FT%TZ) [patch] $*" | tee -a "$LOG"; }
source /root/rubicon.env

PC=http://127.0.0.1:3100

patch_agent() {
  local id="$1" key="$2"
  log "PATCH /api/agents/$id -> agentKey=$key"
  curl -sS -X PATCH "$PC/api/agents/$id" \
    -H "Content-Type: application/json" \
    -d "{
      \"adapterConfig\": {
        \"agentId\": \"$key\",
        \"agentKey\": \"$key\",
        \"role\": \"operator\",
        \"scopes\": [\"operator.admin\"],
        \"paperclipApiUrl\": \"http://127.0.0.1:3100\",
        \"sessionKeyStrategy\": \"issue\",
        \"waitTimeoutMs\": 120000,
        \"omitPaperclipContext\": true
      },
      \"replaceAdapterConfig\": false
    }" | python3 -m json.tool 2>/dev/null | head -40 | tee -a "$LOG"
}

patch_agent "$CEO_ID"   "rubicon_ceo"
patch_agent "$SURG_ID"  "rubicon_surgeon"
patch_agent "$SURG2_ID" "rubicon_surgeon2"

log "verify: GET /api/agents/<ceo>"
curl -sS "$PC/api/agents/$CEO_ID" | python3 -m json.tool 2>/dev/null | head -30 | tee -a "$LOG"
