#!/bin/bash
# How does the openclaw gateway adapter build an agent id?
set +e
PC=/root/paperclip
echo "=== grep for agentId/sessionKey in openclaw-gateway adapter ==="
grep -RIn "agentId\|sessionKey" "$PC/server/src" 2>/dev/null | grep -v node_modules | grep -iE "openclaw|gateway" | head -30
echo ""
echo "=== grep for companySlug combined with agent name ==="
grep -RIn "urlKey\|companyId.*agentName\|slugify\|company.*slug" "$PC/server/src" 2>/dev/null | grep -v node_modules | grep -iE "openclaw|gateway|adapter" | head -30
echo ""
echo "=== existing openclaw registry for tickles-n-co_cody: what's inside? ==="
ls -la /root/.openclaw/agents/tickles-n-co_cody 2>/dev/null
echo ""
echo "=== content of tickles-n-co_cody ==="
find /root/.openclaw/agents/tickles-n-co_cody -type f -exec echo "--- {} ---" \; -exec head -40 {} \;
echo ""
echo "=== similarly for workspace ==="
ls -la /root/.openclaw/workspace/ 2>/dev/null | head -20
echo ""
echo "=== what files are in workspace for an existing agent? (pick first) ==="
WS=$(ls -1 /root/.openclaw/workspace/ 2>/dev/null | head -1)
if [ -n "$WS" ]; then
  echo "[workspace=$WS]"
  ls -la "/root/.openclaw/workspace/$WS" 2>/dev/null
fi
echo ""
echo "=== paperclip companies list (to see what slug Rubicon got) ==="
curl -sS http://127.0.0.1:3100/api/companies | python3 -m json.tool 2>/dev/null | head -40
