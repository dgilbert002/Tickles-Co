#!/usr/bin/env bash
set -u
BASE=http://127.0.0.1:7777/mcp

cat > /tmp/_mcp_create.json <<'EOF'
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"company.create","arguments":{
  "name":"Phase3Test",
  "issuePrefix":"P3T",
  "description":"Phase-3 provisioning smoke test. Safe to delete.",
  "provisioning":{"enabled":true,"template":"blank","slug":"phase3test"}
}}}
EOF

echo "=== company.create (chained Blank provisioning) ==="
curl -s -X POST "$BASE" -H 'content-type: application/json' -d @/tmp/_mcp_create.json \
  | python3 -m json.tool | tee /tmp/_p3_create_out.json

echo
echo "=== checking postgres: is tickles_phase3test there? ==="
sudo -u postgres psql -d postgres -tAc "SELECT datname FROM pg_database WHERE datname = 'tickles_phase3test'"

echo
echo "=== checking qdrant: is collection tickles_phase3test there? ==="
curl -s http://127.0.0.1:6333/collections/tickles_phase3test | python3 -m json.tool | head -20

echo
echo "=== paperclip row metadata ==="
# Pull companyId from the create response
CID=$(python3 -c "import json; d=json.load(open('/tmp/_p3_create_out.json')); print(d['result']['result']['company']['id'])")
echo "companyId=$CID"
curl -s http://127.0.0.1:3100/api/companies/$CID | python3 -m json.tool | grep -E 'metadata|mem0|memu|treasury'

echo
echo "=== CLEANUP: drop db + qdrant collection + paperclip row ==="
sudo -u postgres psql -d postgres -c "DROP DATABASE IF EXISTS tickles_phase3test"
curl -s -X DELETE http://127.0.0.1:6333/collections/tickles_phase3test >/dev/null
curl -s -X DELETE http://127.0.0.1:3100/api/companies/$CID >/dev/null
echo "cleaned up"
