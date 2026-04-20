#!/bin/bash
# Find exactly WHERE OpenClaw's control-UI reads the 8 overlay tabs from.

echo '--- a. content of /root/.openclaw/workspace ---'
sudo ls -la /root/.openclaw/workspace/ 2>/dev/null

echo
echo '--- b. content of /root/.openclaw/identity ---'
sudo ls -la /root/.openclaw/identity/ 2>/dev/null

echo
echo '--- c. ALL *.md files in /root/.openclaw (recursive, limit 30) ---'
sudo find /root/.openclaw -maxdepth 4 -name "*.md" 2>/dev/null | head -30

echo
echo '--- d. where control-UI reads overlays from — grep for "overlay" or "readFile" near AGENT.md ---'
sudo grep -rn "agents/.*AGENT\|workspace/.*AGENT\|readOverlay\|resolveOverlay" /usr/lib/node_modules/openclaw/dist 2>/dev/null | grep -v ".map" | head -15

echo
echo '--- e. look at agents-BB4gX9hg.js around line 646 (where IDENTITY.md is resolved) ---'
sudo sed -n '630,680p' /usr/lib/node_modules/openclaw/dist/agents-BB4gX9hg.js 2>/dev/null

echo
echo '--- f. look at workspace-hhTlRYqM.js for how DEFAULT_TOOLS_FILENAME is joined ---'
sudo grep -n "DEFAULT_TOOLS_FILENAME\|DEFAULT_IDENTITY_FILENAME" /usr/lib/node_modules/openclaw/dist/workspace-hhTlRYqM.js 2>/dev/null | head -10

echo
echo '--- g. API route for tab content: grep "tab" "overlay" in server.impl ---'
sudo grep -n "agent.*overlay\|overlay.*agent\|loadOverlay\|getOverlayFile\|readOverlayFile" /usr/lib/node_modules/openclaw/dist/server.impl*.js 2>/dev/null | head -15
