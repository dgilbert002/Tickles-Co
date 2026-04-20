#!/bin/bash
set +e
LOG=/root/rubicon-deploy.log
log() { echo "$(date -u +%FT%TZ) [diag-oc] $*" | tee -a "$LOG"; }

log "list openclaw agents directory"
ls -la /root/.openclaw/agents/ 2>&1 | tee -a "$LOG"

log "compare cody (working) vs rubicon_surgeon"
echo "--- cody ---" | tee -a "$LOG"
ls -la /root/.openclaw/agents/tickles-n-co_cody/ 2>&1 | tee -a "$LOG"
echo "--- rubicon_surgeon ---" | tee -a "$LOG"
ls -la /root/.openclaw/agents/rubicon_surgeon/ 2>&1 | tee -a "$LOG"

log "cody meta.json"
cat /root/.openclaw/agents/tickles-n-co_cody/meta.json 2>&1 | tee -a "$LOG"
log "rubicon_surgeon meta.json"
cat /root/.openclaw/agents/rubicon_surgeon/meta.json 2>&1 | tee -a "$LOG"

log "check openclaw gateway code - where does it look up agents?"
# The gateway listens on ws://127.0.0.1:18789. Is it a separate openclaw process?
ss -ltnp 2>/dev/null | grep -E "18789|3100|7777" | tee -a "$LOG"

log "who owns 18789?"
lsof -iTCP:18789 -sTCP:LISTEN -nP 2>/dev/null | tee -a "$LOG"

log "systemd openclaw services"
systemctl list-units --type=service --all 2>&1 | grep -iE "openclaw" | tee -a "$LOG"

log "openclaw binary / cli help"
which openclaw | tee -a "$LOG"
openclaw agents list 2>&1 | head -30 | tee -a "$LOG" || true
openclaw --help 2>&1 | head -40 | tee -a "$LOG" || true
