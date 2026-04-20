#!/bin/bash
set +e
echo "=== ccxt_funding.py (first 120 lines) ==="
head -120 /opt/tickles/shared/altdata/sources/ccxt_funding.py 2>/dev/null
echo
echo "=== Is it invoked anywhere? ==="
grep -RIn --include="*.py" "ccxt_funding\|from shared.altdata.sources" /opt/tickles 2>/dev/null | grep -v __pycache__ | head -10
echo
echo "=== openclaw mcp --help ==="
openclaw mcp --help 2>&1 | head -40
echo
echo "=== openclaw mcp add --help ==="
openclaw mcp add --help 2>&1 | head -60
echo
echo "=== Paperclip API routes we care about ==="
curl -s http://127.0.0.1:3100/api/companies 2>/dev/null | head -c 500; echo
echo "--"
curl -s -o /dev/null -w "GET /api/companies: %{http_code}\n" http://127.0.0.1:3100/api/companies
echo
echo "=== Paperclip CLI available? ==="
ls /home/paperclip/paperclip/node_modules/.bin/paperclip* 2>/dev/null
which paperclipai 2>/dev/null
echo
echo "=== MCP daemon /tools endpoint ==="
curl -s http://127.0.0.1:7777/tools 2>/dev/null | head -c 1500; echo
curl -s -o /dev/null -w "GET /tools: %{http_code}\n" http://127.0.0.1:7777/tools
echo
echo "=== MCP daemon root ==="
curl -s http://127.0.0.1:7777/ 2>/dev/null | head -c 500; echo
curl -s -o /dev/null -w "GET /: %{http_code}\n" http://127.0.0.1:7777/
