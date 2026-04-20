#!/bin/bash
set -u
CFG=/root/.openclaw/openclaw.json
cp "$CFG" "$CFG.bak2.$(date -u +%Y%m%dT%H%M%SZ)"

echo "=== switching rubicon_surgeon + _surgeon2 to claude-sonnet-4 ==="
jq '
  (.agents.list[] | select(.id == "rubicon_surgeon") | .model.primary) = "openrouter/anthropic/claude-sonnet-4"
  | (.agents.list[] | select(.id == "rubicon_surgeon2") | .model.primary) = "openrouter/anthropic/claude-sonnet-4"
' "$CFG" > /tmp/cfg_new.json

if jq . /tmp/cfg_new.json > /dev/null 2>&1; then
  mv /tmp/cfg_new.json "$CFG"
  echo "Updated."
else
  echo "Bad JSON"; exit 1
fi

openclaw config validate 2>&1 | tail -5
echo ""

jq '.agents.list[] | select(.id == "rubicon_surgeon" or .id == "rubicon_surgeon2") | {id, model}' "$CFG"
echo ""

echo "=== clear stale sessions (need fresh) ==="
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
for ag in rubicon_surgeon rubicon_surgeon2; do
  dir="/root/.openclaw/agents/$ag/sessions"
  if [ -d "$dir" ]; then
    arc="$dir/_archive_$STAMP"
    mkdir -p "$arc"
    mv "$dir"/*.jsonl "$arc/" 2>/dev/null || true
    mv "$dir"/sessions.json "$arc/" 2>/dev/null || true
  fi
done

echo "=== test claude-sonnet-4 agent turn ==="
timeout 180 openclaw agent --agent rubicon_surgeon --message 'Reply ONLY the word PONG.' --json 2>/dev/null > /tmp/probe_claude.json
jq '{status, text: .result.payloads[0].text, usage: .result.meta.agentMeta.lastCallUsage, durationMs: .result.meta.durationMs, livenessState: .result.meta.livenessState, replayInvalid: .result.meta.replayInvalid, model: .result.meta.agentMeta.model}' /tmp/probe_claude.json
