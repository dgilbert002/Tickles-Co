#!/bin/bash
# READ-ONLY safety check. No writes. No edits.
# Purpose: verify where Whisper + Telegram + Main agent live, and what MCP/tokens are currently configured.

set -u

section() { echo; echo "=========================================="; echo "$1"; echo "=========================================="; }

section "1. OpenClaw workspace dirs"
ls -la /root/.openclaw/ 2>/dev/null | head -40

section "2. OpenClaw workspace/ (per-agent UI folders)"
ls -la /root/.openclaw/workspace/ 2>/dev/null | head -40

section "3. OpenClaw agents/ (per-agent runtime folders)"
ls -la /root/.openclaw/agents/ 2>/dev/null | head -40

section "4. openclaw.json top-level keys (no secrets)"
python3 -c "
import json
with open('/root/.openclaw/openclaw.json') as f:
    data = json.load(f)
print('top-level keys:', sorted(data.keys()))
agents = data.get('agents', {})
print('agents section keys:', sorted(agents.keys()) if isinstance(agents, dict) else type(agents).__name__)
# channels
ch = data.get('channels', {})
if isinstance(ch, dict):
    print('channels defined:', sorted(ch.keys()))
# MCP
mcp = data.get('mcp', {})
if isinstance(mcp, dict):
    servers = mcp.get('servers', {})
    if isinstance(servers, dict):
        print('mcp.servers defined:', sorted(servers.keys()))
# agents.defaults
defaults = agents.get('defaults', {}) if isinstance(agents, dict) else {}
if isinstance(defaults, dict):
    print('agents.defaults keys:', sorted(defaults.keys()))
# per-agent entries (just IDs)
per_agent = agents.get('agents', {}) if isinstance(agents, dict) else {}
if isinstance(per_agent, dict):
    print('per-agent IDs:', sorted(per_agent.keys()))
" 2>&1

section "5. Telegram references in /root/.openclaw/ (filenames only)"
grep -rli "telegram" /root/.openclaw/ 2>/dev/null | head -20

section "6. Whisper references in /root/.openclaw/ (filenames only)"
grep -rli "whisper" /root/.openclaw/ 2>/dev/null | head -20

section "7. Paperclip channels/telegram on disk"
ls /home/paperclip/.paperclip/instances/default/ 2>/dev/null | head -20
grep -rli "telegram\|whisper" /home/paperclip/.paperclip/ 2>/dev/null | head -20

section "8. Running services"
ps -eo pid,cmd --no-headers | grep -iE "paperclip|openclaw|telegram|whisper" | grep -v grep | head -20

section "9. Systemd services"
systemctl list-units --type=service --no-pager 2>/dev/null | grep -iE "paperclip|openclaw|telegram|whisper" | head -20

section "10. Paperclip HTTP quick health"
curl -s -o /dev/null -w "paperclip :3100 http_code=%{http_code}\n" http://127.0.0.1:3100/api/health 2>/dev/null || echo "no response"

section "11. OpenClaw gateway WS port"
ss -lntp 2>/dev/null | grep -E ":18789|:3100|:7008" | head -10

echo; echo "=== DONE (read-only, no writes) ==="
