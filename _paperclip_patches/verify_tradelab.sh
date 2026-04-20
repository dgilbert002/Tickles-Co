#!/usr/bin/env bash
set -euo pipefail

echo "=== Paperclip state (filtered) ==="
python3 /tmp/inspect_state.py 2>&1 | grep -E 'TradeLab|tradelab|CEO|Audrey|Cody|Schemy|Main|Janitor|Strategy|Building|Tickles' | head -40

echo
echo "=== /root/.openclaw/agents/ ==="
sudo ls -la /root/.openclaw/agents/

echo
echo "=== tradelab postgres DB ==="
sudo -u postgres psql -d postgres -tAc "SELECT datname FROM pg_database WHERE datname LIKE 'tickles_tradelab%'"

echo
echo "=== tradelab qdrant collection ==="
curl -sS http://127.0.0.1:6333/collections/tickles_tradelab -w '\nhttp=%{http_code}\n' 2>&1 | head -5

echo
echo "=== tradelab CEO openclaw dir ==="
sudo ls -la /root/.openclaw/agents/tradelab_ceo/ 2>&1 | head -15 || echo "  (missing)"

echo
echo "=== tradelab CEO AGENT.md ==="
sudo cat /root/.openclaw/agents/tradelab_ceo/AGENT.md 2>&1 | head -30 || echo "  (missing)"

echo
echo "=== tradelab CEO adapterConfig.agentId in Paperclip ==="
python3 <<'PY'
import json, urllib.request
r = urllib.request.urlopen("http://127.0.0.1:3100/api/companies", timeout=10)
companies = json.loads(r.read())
if isinstance(companies, dict):
    companies = companies.get("companies") or companies.get("data") or []
tradelab = next((c for c in companies if c.get("name") == "TradeLab"), None)
if not tradelab:
    print("  !! TradeLab not found")
    raise SystemExit(1)
cid = tradelab["id"]
print(f"  companyId = {cid}")
print(f"  slug      = {tradelab.get('slug')}")
r = urllib.request.urlopen(f"http://127.0.0.1:3100/api/companies/{cid}/agents", timeout=10)
ags = json.loads(r.read())
if isinstance(ags, dict):
    ags = ags.get("agents") or ags.get("data") or []
for a in ags:
    cfg = a.get("adapterConfig") or {}
    print(f"  agent: {a['name']!r} role={a.get('role')} urlKey={a.get('urlKey')!r}")
    print(f"    adapterType = {a.get('adapterType')}")
    print(f"    adapterConfig.agentId = {cfg.get('agentId')!r}")
    print(f"    adapterConfig.url     = {cfg.get('url')!r}")
    headers = cfg.get("headers") or {}
    tok = headers.get("x-openclaw-token") if isinstance(headers, dict) else None
    print(f"    adapterConfig.headers[x-openclaw-token] set = {bool(tok)}")
    md = a.get("metadata") or {}
    print(f"    metadata.templateAgent = {md.get('templateAgent')}")
PY
