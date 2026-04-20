#!/usr/bin/env python3
"""Pull full agent history via Paperclip API to see what I overwrote."""
import json
import urllib.request

BASE = "http://127.0.0.1:3100"


def get(path):
    req = urllib.request.Request(f"{BASE}{path}")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


companies = get("/api/companies")
for c in companies:
    cid = c["id"]
    name = c["name"]
    try:
        agents = get(f"/api/companies/{cid}/agents")
    except Exception:
        continue
    print(f"\n======= COMPANY: {name} =======")
    for a in agents:
        aid = a["id"]
        an = a["name"]
        print(f"\n--- AGENT: {an} (id={aid}) ---")
        # latest full agent (includes adapter_config + any secrets redacted fields)
        try:
            detail = get(f"/api/agents/{aid}")
            ac = detail.get("adapterConfig") or detail.get("adapter_config") or {}
            # keep this short but informative
            print(f"  name         : {detail.get('name')}")
            print(f"  url_key      : {detail.get('urlKey')}")
            print(f"  role         : {detail.get('role')}")
            print(f"  title        : {detail.get('title')}")
            print(f"  status       : {detail.get('status')}")
            print(f"  adapter_type : {detail.get('adapterType')}")
            print(f"  adapter_cfg  : openclaw_id={ac.get('agentId')}  keys={list(ac.keys())}")
            print(f"  heartbeat    : {detail.get('runHeartbeat')} / every={detail.get('heartbeatEverySec')}")
        except Exception as e:
            print(f"  detail err: {e}")
        # config revisions
        try:
            revs = get(f"/api/agents/{aid}/config-revisions")
            if isinstance(revs, dict):
                revs = revs.get("revisions") or revs.get("data") or []
            print(f"  history: {len(revs)} revisions")
            for r in revs[:10]:
                rcfg = r.get("config") or {}
                r_oc = ""
                if isinstance(rcfg, dict):
                    rac = rcfg.get("adapter_config") or rcfg.get("adapterConfig") or {}
                    if isinstance(rac, dict):
                        r_oc = rac.get("agentId", "")
                print(
                    f"    rev {r.get('revision'):>3}  created={r.get('createdAt')}  "
                    f"openclaw_id={r_oc}  fields={sorted((rcfg or {}).keys())[:6]}"
                )
        except urllib.error.HTTPError as e:
            if e.code != 404:
                print(f"  revs err: {e}")
        except Exception as e:
            print(f"  revs err: {e}")
