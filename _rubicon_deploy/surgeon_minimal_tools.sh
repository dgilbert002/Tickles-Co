#!/bin/bash
set -u
echo "=== checking which built-in tool names exist (profile minimal etc.) ==="
# Try to run a minimal infer with NO tools to isolate whether the LLM itself works
timeout 60 openclaw infer model run --model openrouter/openai/gpt-4.1 --prompt 'Reply ONLY the word PONG.' 2>&1 | tail -10
echo ""
echo "=== setting rubicon_surgeon tools.profile to minimal (via jq edit) ==="
CFG=/root/.openclaw/openclaw.json
cp "$CFG" "$CFG.bak.$(date -u +%Y%m%dT%H%M%SZ)"

# Scope both surgeon agents to minimal profile so giant MCP bundle isn't attached
jq '(.agents.list[] | select(.id == "rubicon_surgeon" or .id == "rubicon_surgeon2") | .tools) |= (. // {}) + {profile: "minimal"}' \
  "$CFG" > /tmp/openclaw.new.json
if jq . /tmp/openclaw.new.json > /dev/null 2>&1; then
  mv /tmp/openclaw.new.json "$CFG"
  echo "Config updated."
else
  echo "ERROR: produced invalid JSON, keeping backup."
  exit 1
fi
echo ""
echo "=== verify new entries ==="
jq '.agents.list[] | select(.id == "rubicon_surgeon" or .id == "rubicon_surgeon2") | {id, tools}' "$CFG"
echo ""
echo "=== validate config ==="
openclaw config validate 2>&1 | tail -10
echo ""
echo "=== agent test with minimal profile ==="
timeout 90 openclaw agent --agent rubicon_surgeon --message 'Reply ONLY the word PONG.' --json 2>/dev/null > /tmp/probe_minimal.json
jq '{status, text: .result.payloads[0].text, usage: .result.meta.agentMeta.lastCallUsage, durationMs: .result.meta.durationMs, livenessState: .result.meta.livenessState, replayInvalid: .result.meta.replayInvalid}' /tmp/probe_minimal.json
