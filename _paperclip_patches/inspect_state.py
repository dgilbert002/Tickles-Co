#!/usr/bin/env python3
"""Pull the current state of Paperclip + OpenClaw so we can plan cleanup/backfill."""
import json
import os
import urllib.request
import urllib.error

PAPERCLIP = os.environ.get("PAPERCLIP_URL", "http://127.0.0.1:3100")

def http_get(path):
    url = f"{PAPERCLIP}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None

def main():
    status, companies = http_get("/api/companies")
    if status != 200:
        print(f"GET /api/companies failed: {status}")
        return

    # Some versions wrap in {"companies": [...]} — normalize.
    if isinstance(companies, dict):
        companies = companies.get("companies") or companies.get("data") or []

    print("=" * 80)
    print(f"COMPANIES in Paperclip ({len(companies)}):")
    print("=" * 80)
    for c in companies:
        cid = c.get("id")
        name = c.get("name")
        slug = c.get("slug") or c.get("urlKey") or "?"
        print(f"\n  {name!r:38} id={cid} slug={slug!r}")
        _, agents = http_get(f"/api/companies/{cid}/agents")
        if isinstance(agents, dict):
            agents = agents.get("agents") or agents.get("data") or []
        if not agents:
            print("    (no agents)")
            continue
        for a in agents:
            aid = a.get("id")
            aname = a.get("name")
            aurl = a.get("urlKey") or "?"
            role = a.get("role")
            adapter = a.get("adapterType")
            cfg = a.get("adapterConfig") or {}
            agent_id_cfg = cfg.get("agentId")
            agent_key = cfg.get("agentKey")
            url_set = bool(cfg.get("url"))
            tok = (cfg.get("headers") or {}).get("x-openclaw-token") if isinstance(cfg.get("headers"), dict) else None
            tok_set = bool(tok)
            print(f"    - {aname!r:34} id={aid}")
            print(f"       urlKey={aurl!r} role={role} adapter={adapter}")
            print(f"       adapterConfig.agentId={agent_id_cfg!r} agentKey={agent_key!r}  url={url_set} token={tok_set}")

if __name__ == "__main__":
    main()
