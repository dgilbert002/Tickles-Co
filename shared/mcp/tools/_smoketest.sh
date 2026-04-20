#!/bin/bash
set -euo pipefail
BASE=http://127.0.0.1:7777/mcp

call() {
  local name="$1"; shift
  local args="${1-}"
  if [ -z "$args" ]; then args='{}'; fi
  curl -s -X POST "$BASE" -H "content-type: application/json" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$name\",\"arguments\":$args}}"
}

echo "=== /healthz ==="
curl -s http://127.0.0.1:7777/healthz; echo

echo "=== tools/list count ==="
curl -s -X POST "$BASE" -H "content-type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d["result"]["tools"]))'

echo
echo "=== ping ==="
call ping | python3 -m json.tool

echo
echo "=== company.list ==="
call company.list > /tmp/_mcp_out.json
python3 <<'PY'
import json
d = json.load(open("/tmp/_mcp_out.json"))
r = d["result"]["result"]
print("count=", r["count"])
for c in r["companies"]:
    print(c["id"], c["name"])
PY

echo
echo "=== banker.snapshot (Tickles n Co) ==="
call banker.snapshot '{"companyId":"1def5087-1267-4bfc-8c99-069685fff525"}' > /tmp/_mcp_out.json
python3 <<'PY'
import json
d = json.load(open("/tmp/_mcp_out.json"))
r = d["result"]["result"]
print("cost.spendCents=", r["cost"]["spendCents"])
print("byAgent count=", len(r["byAgent"]))
PY

echo
echo "=== autopsy.run (dry) ==="
call autopsy.run '{"tradeId":"demo-1","symbol":"BTC/USDT","side":"buy","companyId":"test","agentId":"twilly"}' > /tmp/_mcp_out.json
python3 <<'PY'
import json
d = json.load(open("/tmp/_mcp_out.json"))
r = d["result"]["result"]
print("template=", r["template"])
print("prompt_lines=", len(r["prompt"].splitlines()))
print("memory_scope=", r["memory_write_hint"]["scope"])
PY

echo
echo "=== memory.add payload shape ==="
call memory.add '{"scope":"agent","companyId":"tickles","agentId":"audrey","content":"unit test memory"}' \
  | python3 -m json.tool

echo
echo "=== execution.submit (expect not_implemented) ==="
call execution.submit '{"companyId":"x","agentId":"y","venue":"paper","symbol":"BTC/USDT","side":"buy","quantity":0.001}' > /tmp/_mcp_out.json
python3 <<'PY'
import json
d = json.load(open("/tmp/_mcp_out.json"))
r = d["result"]["result"]
print("status=", r["status"])
print("feature=", r["feature"])
PY
