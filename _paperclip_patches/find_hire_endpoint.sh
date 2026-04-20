#!/bin/bash
set +e
F=/home/paperclip/paperclip/server/src/routes/agents.ts
echo "=== agents.ts POST endpoints (lines 841 and 913 regions) ==="
sed -n '835,925p' "$F"
echo "---"
sed -n '910,975p' "$F"
echo
echo "=== any 'hire' route? ==="
grep -n "hire\|/agents\b" "$F" | head -20
echo
echo "=== delete the test company ==="
curl -s -X DELETE http://127.0.0.1:3100/api/companies/2007c3cc-6156-4c24-9a55-fc5776bbff93 | head -c 400; echo
curl -s http://127.0.0.1:3100/api/companies
echo
