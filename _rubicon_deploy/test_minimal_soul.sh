#!/bin/bash
set -u
WS=/root/.openclaw/workspace/rubicon_surgeon
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

echo "=== backing up SOUL.md etc ==="
for f in SOUL.md AGENTS.md TRADE_LOG.md TRADE_STATE.md MARKET_STATE.json MARKET_INDICATORS.json BOOTSTRAP.md HEARTBEAT.md IDENTITY.md TOOLS.md USER.md; do
  if [ -f "$WS/$f" ]; then
    cp "$WS/$f" "$WS/$f.bak.$STAMP"
  fi
done

echo "=== install a minimal SOUL.md (control test) ==="
cat > "$WS/SOUL.md" <<'EOF'
# Surgeon

You are a helpful assistant. Respond to the user in plain English.
EOF

# Also clear trade_log and other heavy files temporarily
: > "$WS/TRADE_LOG.md"
cat > "$WS/TRADE_STATE.md" <<'EOF'
# TRADE_STATE

No open positions.
EOF

# Nuke sessions
SDIR=/root/.openclaw/agents/rubicon_surgeon/sessions
ARC=$SDIR/_archive_$STAMP
mkdir -p "$ARC"
mv "$SDIR"/*.jsonl "$ARC/" 2>/dev/null || true
mv "$SDIR"/sessions.json "$ARC/" 2>/dev/null || true

echo "=== test with minimal SOUL ==="
timeout 180 openclaw agent --agent rubicon_surgeon --message 'Reply ONLY the word PONG.' --json 2>/dev/null > /tmp/probe_minimal_soul.json
jq '{text: .result.payloads[0].text, usage: .result.meta.agentMeta.lastCallUsage, durationMs: .result.meta.durationMs, livenessState: .result.meta.livenessState, model: .result.meta.agentMeta.model}' /tmp/probe_minimal_soul.json
