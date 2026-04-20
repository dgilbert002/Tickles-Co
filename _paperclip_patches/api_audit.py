#!/usr/bin/env python3
"""Query Paperclip HTTP API for the REAL agent listing."""
import json
import urllib.request

BASE = "http://127.0.0.1:3100"


def get(path):
    req = urllib.request.Request(f"{BASE}{path}")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


companies = get("/api/companies")
print(f"COMPANIES: {len(companies)}")
for c in companies:
    print(f"  {c['id']}  name={c['name']:<20} status={c['status']}  created={c['createdAt']}")

print()
for c in companies:
    cid = c["id"]
    name = c["name"]
    try:
        agents = get(f"/api/companies/{cid}/agents")
    except Exception as e:
        print(f"agents for {name}: ERR {e}")
        continue
    print(f"\nCOMPANY {name} — {len(agents)} agents:")
    for a in agents:
        ac = a.get("adapterConfig") or a.get("adapter_config") or {}
        oc = ac.get("agentId") if isinstance(ac, dict) else None
        status = a.get("status")
        url_key = a.get("urlKey") or a.get("url_key")
        created = a.get("createdAt") or a.get("created_at")
        updated = a.get("updatedAt") or a.get("updated_at")
        title = a.get("title") or ""
        role = a.get("role") or ""
        print(
            f"  id={a.get('id')}  name={a.get('name'):<30} url_key={url_key:<40} "
            f"openclaw_id={str(oc):<35} status={status}  created={created}  "
            f"updated={updated}  role={role}  title={title}"
        )
