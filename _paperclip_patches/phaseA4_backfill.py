#!/usr/bin/env python3
"""Phase A.4 — backfill existing openclaw_gateway agents with the new
auto-defaults for url + x-openclaw-token by PATCHing each one with an empty
adapterConfig, which triggers Paperclip's applyCreateDefaultsByAdapterType
merge path."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

PAPERCLIP = "http://127.0.0.1:3100"


def _get(url: str):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.load(resp)


def _patch(url: str, body: dict):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="PATCH",
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def main() -> int:
    print("=== companies ===")
    comps = _get(f"{PAPERCLIP}/api/companies")
    if isinstance(comps, dict):
        comps = comps.get("items", [])
    for c in comps:
        print(f"  {c['id']}\t{c.get('name')}")

    broken: list[dict] = []
    print("\n=== openclaw_gateway agents ===")
    for c in comps:
        cid = c["id"]
        try:
            agents = _get(f"{PAPERCLIP}/api/companies/{cid}/agents")
        except Exception as err:
            print(f"  [warn] company={cid}: {err}")
            continue
        if isinstance(agents, dict):
            agents = agents.get("items", [])
        for a in agents:
            if a.get("adapterType") != "openclaw_gateway":
                continue
            cfg = a.get("adapterConfig") or {}
            hdrs = cfg.get("headers") or {}
            has_url = bool(cfg.get("url"))
            has_tok = bool(hdrs.get("x-openclaw-token") or hdrs.get("x-openclaw-auth"))
            marker = "OK  " if (has_url and has_tok) else "FIX "
            print(f"  {marker} {a['id']}  name={a.get('name'):<20} "
                  f"company={c.get('name'):<20} url={has_url} token={has_tok}")
            if not (has_url and has_tok):
                broken.append({
                    "id": a["id"],
                    "name": a.get("name"),
                    "companyId": cid,
                })

    if not broken:
        print("\nNothing to backfill.")
        return 0

    print(f"\n=== backfilling {len(broken)} agent(s) ===")
    for a in broken:
        try:
            resp = _patch(
                f"{PAPERCLIP}/api/agents/{a['id']}",
                {"adapterConfig": {}},
            )
            cfg = resp.get("adapterConfig") or {}
            hdrs = cfg.get("headers") or {}
            print(f"  [ok] {a['name']:<20} url={bool(cfg.get('url'))} "
                  f"token={bool(hdrs.get('x-openclaw-token'))}")
        except urllib.error.HTTPError as err:
            print(f"  [fail] {a['name']}: HTTP {err.code} "
                  f"{err.read().decode('utf-8', 'replace')[:240]}")
        except Exception as err:
            print(f"  [fail] {a['name']}: {err}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
