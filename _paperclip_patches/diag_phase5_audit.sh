#!/bin/bash
# Phase 5 audit — what the user reported:
#   1. Duplicates in dropdown (main + cody + schemy + audrey AND tickles-n-co_*)
#   2. MISSING badges on TOOLS/IDENTITY/USER/HEARTBEAT/BOOTSTRAP/MEMORY for
#      tickles-n-co_schemy in the UI.
#   3. Failed runs: "invalid agent params: unknown agent id
#      tickles-n-co_audrey/cody/schemy" — then started working after user
#      saved.

echo '=========================================================='
echo '1a. LEGACY cody (pre-phase-5, works) — file listing'
echo '=========================================================='
sudo ls -la /root/.openclaw/agents/cody/ 2>/dev/null || echo '(no dir)'
echo
echo '=========================================================='
echo '1b. NEW tickles-n-co_schemy — file listing'
echo '=========================================================='
sudo ls -la /root/.openclaw/agents/tickles-n-co_schemy/ 2>/dev/null || echo '(no dir)'
echo
echo '=========================================================='
echo '2. Paperclip agents for Tickles n Co company'
echo '=========================================================='
sudo -u postgres psql -d paperclip -A -c "SELECT id, url_key, name, adapter_type, adapter_config->>'agentId' AS oc_id, created_at FROM agents WHERE company_id='1def5087-1267-4bfc-8c99-069685fff525' ORDER BY created_at;" 2>/dev/null
echo
echo '=========================================================='
echo '3. openclaw.json agents.list — DOES legacy cody exist?'
echo '=========================================================='
sudo python3 -c "
import json
d = json.load(open('/root/.openclaw/openclaw.json'))
for a in d.get('agents', {}).get('list', []):
    print('id=', a.get('id'), ' hb=', a.get('heartbeat', {}).get('every', '-'))
"
echo
echo '=========================================================='
echo '4. tickles-meta-map.json — who owns each new agent?'
echo '=========================================================='
sudo cat /root/.openclaw/tickles-meta-map.json 2>/dev/null | head -60
