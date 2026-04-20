#!/bin/bash
set +e
LOG=/root/rubicon-deploy.log
log() { echo "$(date -u +%FT%TZ) [c5] $*" | tee -a "$LOG"; }
source /root/rubicon.env

register_agent() {
  local name="$1"
  local model="$2"
  log "== register $name =="
  local wsdir="/root/.openclaw/workspace/$name"
  local adir="/root/.openclaw/agents/$name"
  mkdir -p "$wsdir" "$adir"

  openclaw agents add "$name" \
    --non-interactive \
    --workspace "$wsdir" \
    --agent-dir "$adir" \
    --model "$model" \
    --json 2>&1 | tee -a "$LOG"
}

register_agent rubicon_ceo       "openrouter/anthropic/claude-sonnet-4"
register_agent rubicon_surgeon   "openrouter/anthropic/claude-sonnet-4"
register_agent rubicon_surgeon2  "openrouter/anthropic/claude-sonnet-4"

log "=== verify ==="
openclaw agents list --json 2>&1 | grep -E "rubicon_" | tee -a "$LOG"
