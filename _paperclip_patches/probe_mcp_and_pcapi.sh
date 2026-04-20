#!/bin/bash
set +e
echo "=== 1. MCP daemon transport probe ==="
# Typical MCP-over-HTTP endpoints to try:
for ep in /health /healthz /ping /status /mcp /sse /messages /api/tools /list_tools /v1/tools; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "http://127.0.0.1:7777${ep}")
  echo "  GET ${ep} -> ${CODE}"
done
echo
echo "--- /health content ---"
curl -s --max-time 2 http://127.0.0.1:7777/health 2>/dev/null | head -c 400; echo
echo
echo "=== 2. What does tickles-mcpd actually bind to and how does it talk? ==="
head -60 /opt/tickles/shared/mcp/bin/tickles_mcpd.py 2>/dev/null
echo
echo "=== 3. Paperclip API endpoints ==="
for path in /api/health /api/companies /api/agents /api/issues /api/instances /api/adapters; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "http://127.0.0.1:3100${path}")
  echo "  GET ${path} -> ${CODE}"
done
echo
echo "--- GET /api/health ---"
curl -s --max-time 2 http://127.0.0.1:3100/api/health 2>/dev/null | head -c 600; echo
echo "--- GET /api/adapters ---"
curl -s --max-time 2 http://127.0.0.1:3100/api/adapters 2>/dev/null | head -c 800; echo
echo "--- GET /api/companies ---"
curl -s --max-time 2 http://127.0.0.1:3100/api/companies 2>/dev/null | head -c 400; echo
echo
echo "=== 4. Paperclip routes discovery (from source) ==="
grep -RIn --include="*.ts" -E "router\.(get|post|put|delete|patch)\(['\"]/" /home/paperclip/paperclip/server/src 2>/dev/null | grep -iE "companies|agents|issues|hire|heartbeat" | head -30
echo
echo "=== 5. Paperclip companies schema (from migrations or models) ==="
grep -RIn --include="*.ts" -E "create_table|CREATE TABLE|pgTable.*companies" /home/paperclip/paperclip/server/src 2>/dev/null | head -10
echo
echo "=== 6. How does Paperclip authenticate API calls? ==="
grep -RIn --include="*.ts" "bearer\|authorization\|api[_-]key\|x-paperclip" /home/paperclip/paperclip/server/src/api 2>/dev/null | head -15
echo
echo "=== 7. Find 'create company' or 'hire agent' endpoints ==="
grep -RIn --include="*.ts" -E "POST.*compan|post.*agent|create.*compan|hire" /home/paperclip/paperclip/server/src 2>/dev/null | head -20
