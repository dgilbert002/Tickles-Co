#!/usr/bin/env bash
set -e
echo "=== Paperclip openapi routes containing 'issue' ==="
curl -sS http://127.0.0.1:3100/openapi.json 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
paths = d.get("paths", {})
for p, ops in paths.items():
    if "issue" in p.lower() or "task" in p.lower() or "mandate" in p.lower():
        for m in ops.keys():
            if m in ("get","post","patch","put","delete"):
                print(f"  {m.upper():6s} {p}")
' 2>&1 | head -40 || echo "(openapi not available)"

echo
echo "=== Paperclip routes listing (fallback) ==="
for path in /api/issues /api/v1/issues /api/companies /api/health ; do
    echo ">>> GET ${path}"
    curl -sS -o /tmp/_r.json -w 'http=%{http_code}\n' "http://127.0.0.1:3100${path}"
    head -c 500 /tmp/_r.json
    echo
    echo
done

echo
echo "=== MCP tools/list (check for backtest.*) ==="
curl -sS -X POST http://127.0.0.1:7777/mcp \
    -H "content-type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
    2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
tools = d.get("result", {}).get("tools", [])
print(f"total tools: {len(tools)}")
for t in tools:
    name = t.get("name","?")
    if any(k in name for k in ("backtest","trade","issue","autopsy","postmortem","feedback","banker","treasury","execution","md.","catalog","memory","memu","learning","ping","altdata","agent.","company.")):
        desc = (t.get("description","") or "").split("\n",1)[0][:80]
        print(f"  {name:35s} | {desc}")
'
