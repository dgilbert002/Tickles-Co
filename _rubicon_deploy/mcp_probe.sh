#!/bin/bash
set -u
echo "=== openclaw mcp help ==="
openclaw mcp --help 2>&1 | head -40
echo ""
echo "=== openclaw.json 'mcp' + 'tools' + 'agents.defaults' ==="
jq '{mcp, tools, agents_defaults: .agents.defaults}' /root/.openclaw/openclaw.json 2>&1 | head -120
echo ""
echo "=== full agent entry for rubicon_surgeon ==="
jq '.agents.list[] | select(.id == "rubicon_surgeon")' /root/.openclaw/openclaw.json 2>&1
echo ""
echo "=== full agent entry for main ==="
jq '.agents.list[] | select(.id == "main")' /root/.openclaw/openclaw.json 2>&1
