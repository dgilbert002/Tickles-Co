#!/bin/bash
set -u
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

echo "=== preserving workspace data ==="
# workspace has TRADE_LOG, MARKET_STATE etc - archive before agent delete (which prunes workspace)
SRC=/root/.openclaw/workspace/rubicon_surgeon
BAK=/root/.openclaw/_archive_workspace_rubicon_surgeon_$STAMP
if [ -d "$SRC" ]; then
  cp -a "$SRC" "$BAK"
  echo "Backed up $SRC -> $BAK"
fi

echo "=== deleting agent (clears all cached state) ==="
openclaw agents delete rubicon_surgeon --force --json 2>&1 | tail -5

echo ""
echo "=== recreating agent fresh ==="
openclaw agents add rubicon_surgeon \
  --workspace /root/.openclaw/workspace/rubicon_surgeon \
  --model openrouter/anthropic/claude-sonnet-4 \
  --non-interactive \
  --json 2>&1 | tail -15

echo ""
echo "=== restore workspace files (TRADE_LOG, MARKET_STATE, etc.) ==="
if [ -d "$BAK" ]; then
  # Restore user files but NOT .openclaw/.git subdirs (fresh agent manages those)
  for f in TRADE_LOG.md TRADE_STATE.md MARKET_STATE.json MARKET_INDICATORS.json .surgeon_state.json .llm_budget.json config.json; do
    if [ -f "$BAK/$f" ]; then
      cp "$BAK/$f" "$SRC/$f"
      echo "restored $f"
    fi
  done
fi

echo ""
echo "=== install Twilly SOUL (full, from the template) ==="
if [ -f /opt/tickles/shared/templates/trading_agent/SOUL.template.md ]; then
  sed "s/{{AGENT_NAME}}/rubicon_surgeon/g" /opt/tickles/shared/templates/trading_agent/SOUL.template.md > "$SRC/SOUL.md"
  echo "SOUL.md installed ($(wc -c < "$SRC/SOUL.md") chars)"
fi

echo ""
echo "=== verify agent ==="
openclaw agents list 2>&1 | grep -A4 '^- rubicon_surgeon'

echo ""
echo "=== TEST: simple PONG (with full Twilly SOUL and workspace files restored) ==="
timeout 180 openclaw agent --agent rubicon_surgeon --message 'Reply ONLY the word PONG.' --json 2>/dev/null > /tmp/probe_fresh.json
jq '{text: .result.payloads[0].text, usage: .result.meta.agentMeta.lastCallUsage, durationMs: .result.meta.durationMs, livenessState: .result.meta.livenessState, model: .result.meta.agentMeta.model}' /tmp/probe_fresh.json
