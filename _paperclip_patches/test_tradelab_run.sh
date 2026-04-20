#!/usr/bin/env bash
set -euo pipefail

echo "=== OpenClaw agent registry (on disk) ==="
sudo ls /root/.openclaw/agents/ | sort

echo
echo "=== OpenClaw gateway /agents endpoint (if one exists) ==="
TOKEN=$(sudo cat /etc/paperclip/openclaw-gateway.env | awk -F= '/OPENCLAW_GATEWAY_TOKEN/{print $2}' | tr -d '"')
echo "gateway token len = ${#TOKEN}"
curl -sS http://127.0.0.1:18789/agents -H "x-openclaw-token: ${TOKEN}" -w '\nhttp=%{http_code}\n' 2>&1 | head -40 || true
echo
curl -sS http://127.0.0.1:18789/agents/tradelab_ceo -H "x-openclaw-token: ${TOKEN}" -w '\nhttp=%{http_code}\n' 2>&1 | head -40 || true

echo
echo "=== Paperclip list agent actions for TradeLab CEO ==="
python3 <<'PY'
import json, urllib.request
r = urllib.request.urlopen("http://127.0.0.1:3100/api/companies", timeout=10)
companies = json.loads(r.read())
if isinstance(companies, dict):
    companies = companies.get("companies") or companies.get("data") or []
tradelab = next((c for c in companies if c.get("name") == "TradeLab"), None)
cid = tradelab["id"]
r = urllib.request.urlopen(f"http://127.0.0.1:3100/api/companies/{cid}/agents", timeout=10)
ags = json.loads(r.read())
if isinstance(ags, dict):
    ags = ags.get("agents") or ags.get("data") or []
ceo = ags[0]
aid = ceo["id"]
print(f"ceo agent id = {aid}")

# Try known Paperclip adapter probe endpoint
for path in (f"/api/agents/{aid}/probe", f"/api/agents/{aid}/adapter/probe", f"/api/agents/{aid}"):
    try:
        url = f"http://127.0.0.1:3100{path}"
        r = urllib.request.urlopen(url, timeout=10)
        body = r.read().decode()[:800]
        print(f"GET {path} -> {r.status}")
        print(f"  {body}")
    except urllib.error.HTTPError as e:
        print(f"GET {path} -> {e.code} :: {e.read().decode()[:300]}")
    except Exception as exc:
        print(f"GET {path} -> err {exc}")
PY
