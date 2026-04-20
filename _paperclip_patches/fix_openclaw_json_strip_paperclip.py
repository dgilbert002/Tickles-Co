#!/usr/bin/env python3
"""Emergency fix: OpenClaw's openclaw.json schema rejects the `paperclip`
key we added to agents.list[] entries. Strip it from every entry and
relocate the metadata to a side-file we OWN (agents/meta-map.json) so we
still have a companySlug / role lookup for our own backfill code.

This script MUST be idempotent — running it twice is a no-op.
"""
import json
import os
import shutil
import sys
import datetime as dt

CFG = "/root/.openclaw/openclaw.json"
SIDE_MAP = "/root/.openclaw/tickles-meta-map.json"

raw = open(CFG, "r", encoding="utf-8").read()
cfg = json.loads(raw)

# Backup before any mutation.
stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
backup = f"{CFG}.bak.strip-paperclip-{stamp}"
shutil.copy2(CFG, backup)
print(f"[fix] backup -> {backup}")

lst = cfg.get("agents", {}).get("list", [])
meta_map: dict = {}
if os.path.exists(SIDE_MAP):
    try:
        meta_map = json.loads(open(SIDE_MAP, "r", encoding="utf-8").read())
    except Exception:
        meta_map = {}

stripped = 0
for entry in lst:
    if not isinstance(entry, dict):
        continue
    if "paperclip" in entry:
        meta_map[entry["id"]] = entry.pop("paperclip")
        stripped += 1
print(f"[fix] stripped 'paperclip' from {stripped} entries")
print(f"[fix] meta_map now holds {len(meta_map)} ids in {SIDE_MAP}")

with open(SIDE_MAP, "w", encoding="utf-8") as f:
    f.write(json.dumps(meta_map, indent=2) + "\n")

tmp = f"{CFG}.tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(json.dumps(cfg, indent=2) + "\n")
os.replace(tmp, CFG)
print(f"[fix] wrote sanitized {CFG}")
