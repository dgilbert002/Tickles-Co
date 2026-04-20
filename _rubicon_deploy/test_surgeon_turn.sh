#!/bin/bash
set -u
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
echo "=== nuking stale sessions ==="
for ag in rubicon_surgeon rubicon_surgeon2; do
  dir="/root/.openclaw/agents/$ag/sessions"
  if [ -d "$dir" ]; then
    arc="$dir/_archive_$STAMP"
    mkdir -p "$arc"
    mv "$dir"/*.jsonl "$arc/" 2>/dev/null || true
    mv "$dir"/sessions.json "$arc/" 2>/dev/null || true
    echo "$ag cleaned. Remaining:"
    ls "$dir/" 2>&1 | grep -v _archive || echo "(clean)"
  fi
done
echo ""
echo "=== TEST: fresh turn on rubicon_surgeon ==="
timeout 120 openclaw agent --agent rubicon_surgeon --message 'Reply with just the word PONG. Nothing else.' --json 2>/dev/null > /tmp/agent_test2.json
jq '{status, summary, text: .result.payloads[0].text, usage: .result.meta.agentMeta.lastCallUsage, durationMs: .result.meta.durationMs, livenessState: .result.meta.livenessState, replayInvalid: .result.meta.replayInvalid}' /tmp/agent_test2.json 2>&1
