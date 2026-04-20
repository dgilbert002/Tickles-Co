#!/usr/bin/env bash
# Phase A.4 — Backfill existing broken openclaw_gateway agents with the new
# auto-defaults. Triggers Paperclip's PATCH→applyCreateDefaultsByAdapterType
# merge path by sending an empty adapterConfig (which merges with the existing
# one and then injects url + x-openclaw-token on the way through).
set -euo pipefail

PAPERCLIP=http://127.0.0.1:3100

echo "== 1. list companies =="
curl -sS "$PAPERCLIP/api/companies" | python3 -c '
import json, sys
d = json.load(sys.stdin)
items = d if isinstance(d, list) else d.get("items", [])
for c in items:
    print(c["id"], "\t", c.get("name"))
'

echo
echo "== 2. list all openclaw_gateway agents across companies =="
AGENTS_JSON=$(curl -sS "$PAPERCLIP/api/companies" | python3 -c '
import json, sys, urllib.request
d = json.load(sys.stdin)
items = d if isinstance(d, list) else d.get("items", [])
out = []
for c in items:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:3100/api/companies/{c[\"id\"]}/agents", timeout=5) as r:
            agents = json.load(r)
            if isinstance(agents, dict):
                agents = agents.get("items", [])
            for a in agents:
                if a.get("adapterType") == "openclaw_gateway":
                    cfg = a.get("adapterConfig") or {}
                    has_url = bool(cfg.get("url"))
                    hdrs = cfg.get("headers") or {}
                    has_tok = bool(hdrs.get("x-openclaw-token") or hdrs.get("x-openclaw-auth"))
                    out.append({
                        "agentId": a["id"], "name": a.get("name"), "companyId": c["id"],
                        "hasUrl": has_url, "hasToken": has_tok,
                    })
    except Exception as err:
        print(f"# warn: company {c.get(\"id\")}: {err}", file=sys.stderr)
print(json.dumps(out, indent=2))
')
echo "$AGENTS_JSON"

echo
echo "== 3. PATCH each broken agent with adapterConfig={} to trigger merge =="
echo "$AGENTS_JSON" | python3 -c '
import json, sys, urllib.request, urllib.error
agents = json.loads(sys.stdin.read())
for a in agents:
    if a["hasUrl"] and a["hasToken"]:
        print(f"[skip] {a[\"name\"]} already has url+token")
        continue
    aid = a["agentId"]
    body = json.dumps({"adapterConfig": {}}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:3100/api/agents/{aid}",
        data=body, method="PATCH",
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.load(r)
            cfg = resp.get("adapterConfig", {})
            hdrs = (cfg.get("headers") or {})
            ok_url = bool(cfg.get("url"))
            ok_tok = bool(hdrs.get("x-openclaw-token"))
            print(f"[patched] {a[\"name\"]:20} -> url={ok_url} token={ok_tok}")
    except urllib.error.HTTPError as err:
        print(f"[fail] {a[\"name\"]}: HTTP {err.code} {err.read().decode()[:200]}")
    except Exception as err:
        print(f"[fail] {a[\"name\"]}: {err}")
'
echo "== done =="
