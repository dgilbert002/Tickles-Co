#!/bin/bash
set +e
LOG=/root/rubicon-deploy.log
log() { echo "$(date -u +%FT%TZ) [runs2] $*" | tee -a "$LOG"; }
source /root/rubicon.env
PC=http://127.0.0.1:3100

log "find run routes in paperclip"
grep -RIn "router\.get.*runs\|agentInvocation\|/runs\"" /root/paperclip/server/src/routes/ 2>/dev/null | head -20 | tee -a "$LOG"

log "check agent's invocation history"
curl -sS "$PC/api/companies/$COMPANY_ID/invocations?agentId=$SURG_ID&limit=3" | head -c 3000 | tee -a "$LOG"
echo "" | tee -a "$LOG"

log "run logs dir?"
ls -la /home/paperclip/.paperclip/instances/default/runs/ 2>/dev/null | head -20 | tee -a "$LOG"

log "systemd services related to openclaw / paperclip"
systemctl list-units --type=service --all 2>&1 | grep -iE "openclaw|paperclip|gateway" | tee -a "$LOG"

log "paperclip logs (last 50)"
journalctl -u paperclip -n 50 --no-pager 2>/dev/null | tail -40 | tee -a "$LOG"
