#!/usr/bin/env python3
"""Quick dump of openclaw.json agents.list[] for verification."""
import json
import sys

CFG = "/root/.openclaw/openclaw.json"

d = json.load(open(CFG))
lst = d.get("agents", {}).get("list", [])
print(f"{len(lst)} agents in openclaw.json:")
for a in lst:
    if not isinstance(a, dict):
        continue
    aid = a.get("id", "?")
    model = a.get("model", {}).get("primary", "?")
    hb = a.get("heartbeat", {}).get("every", "-")
    print(f" - id={aid:<45} model={model:<35} hb={hb}")
