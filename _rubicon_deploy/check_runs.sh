#!/bin/bash
set +e
LOG=/root/rubicon-deploy.log
log() { echo "$(date -u +%FT%TZ) [runs] $*" | tee -a "$LOG"; }
source /root/rubicon.env
PC=http://127.0.0.1:3100

for id in "$SURG_ID" "$SURG2_ID"; do
  log "=== agent $id ==="
  curl -sS "$PC/api/agents/$id/runs?limit=3" | python3 -m json.tool 2>/dev/null | head -80 | tee -a "$LOG"
done

log "=== run 1 error detail (surgeon v1) ==="
RUN1_ID=$(curl -sS "$PC/api/agents/$SURG_ID/runs?limit=1" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['id'] if d else '')")
if [ -n "$RUN1_ID" ]; then
  curl -sS "$PC/api/runs/$RUN1_ID" | python3 -m json.tool 2>/dev/null | head -100 | tee -a "$LOG"
fi

log "=== openclaw gateway logs ==="
journalctl -u openclaw-gateway -n 60 --no-pager 2>/dev/null | tail -40 | tee -a "$LOG"
