#!/usr/bin/env python3
"""Dump the actual historical adapter_config values so we know what to restore."""
import json
import urllib.request

BASE = "http://127.0.0.1:3100"


def get(path):
    req = urllib.request.Request(f"{BASE}{path}")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


# Just focus on Tickles n Co + Building + TradeLab and walk revisions
agents_to_check = [
    ("6b80164f-f995-4f82-b7c4-ae6bebc2f6f2", "Main (Tickles n Co)"),
    ("1ba731d5-6d60-4dd8-b62e-0ba1acdf01c2", "Cody (Tickles n Co)"),
    ("54202a54-321c-44d6-8006-66893d614a8c", "Schemy (Tickles n Co)"),
    ("d2039856-1265-4f9a-bc7e-c458eef39813", "Audrey (Tickles n Co)"),
    ("5d4a953a-ca83-4633-b656-106b971f64d9", "CEO (Building)"),
    ("cc379fb0-c61a-4f4a-8b30-6a89f7e0cb6e", "Janitor (Building)"),
    ("102b42bb-3758-45ee-9682-dcb618f85cd5", "Strategy Council Moderator (Building)"),
]

for aid, label in agents_to_check:
    print(f"\n{'='*70}\nAGENT {label}  (id={aid})\n{'='*70}")
    try:
        revs = get(f"/api/agents/{aid}/config-revisions")
    except Exception as e:
        print(f"  err: {e}")
        continue
    if isinstance(revs, dict):
        revs = revs.get("revisions") or revs.get("data") or []
    if not revs:
        print("  (no revisions)")
        continue
    for r in revs:
        # revision config contains the snapshot
        cfg = r.get("config") or r.get("snapshot") or {}
        created = r.get("createdAt", "?")
        rev_num = r.get("revision", "?")
        kind = r.get("kind") or r.get("changeReason") or ""
        if isinstance(cfg, dict):
            ac = cfg.get("adapter_config") or cfg.get("adapterConfig") or {}
            oc_id = ac.get("agentId") if isinstance(ac, dict) else None
            name = cfg.get("name")
            url_key = cfg.get("url_key") or cfg.get("urlKey")
            print(f"  rev={rev_num}  created={created}  kind={kind}")
            print(f"    name={name}  url_key={url_key}")
            print(f"    adapter_config.agentId={oc_id}")
            # show first couple cfg keys so we know what shape it is
            print(f"    cfg keys={list(cfg.keys())[:10]}")
