#!/usr/bin/env bash
set -u
BASE=http://127.0.0.1:7777/mcp

# tools/list
cat > /tmp/_mcp_list.json <<'EOF'
{"jsonrpc":"2.0","id":1,"method":"tools/list"}
EOF

echo "=== tools/list count ==="
curl -s -X POST "$BASE" -H 'content-type: application/json' -d @/tmp/_mcp_list.json \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); tools=d["result"]["tools"]; print("total=",len(tools)); [print("  "+t["name"]) for t in tools if t["name"].startswith("company.")]'

# company.templates
cat > /tmp/_mcp_templates.json <<'EOF'
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"company.templates","arguments":{}}}
EOF

echo
echo "=== company.templates ==="
curl -s -X POST "$BASE" -H 'content-type: application/json' -d @/tmp/_mcp_templates.json \
  | python3 -m json.tool
