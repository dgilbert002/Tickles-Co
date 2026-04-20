#!/usr/bin/env python3
"""Phase 5 audit v2 — answers the user's three concerns:
  1. Duplication in UI dropdown (legacy cody vs tickles-n-co_cody).
  2. MISSING badges on TOOLS/IDENTITY/etc tabs in OpenClaw UI.
  3. Old failed runs with "unknown agent id".
"""
import json
import subprocess
from pathlib import Path

OC = Path("/root/.openclaw")
print("=" * 70)
print("Q1. DUPLICATION — what's in each folder?")
print("=" * 70)
agents_dir = OC / "agents"
if agents_dir.exists():
    for d in sorted(agents_dir.iterdir()):
        if d.is_dir():
            mds = sorted(p.name for p in d.iterdir() if p.suffix == ".md")
            age_hint = "NEW (phase5)" if "_" in d.name else "LEGACY (pre-phase5)"
            print(f"  {d.name:<45} {age_hint}  mds={mds}")

print()
print("=" * 70)
print("Q2. MISSING BADGES — does OpenClaw look at a DIFFERENT path?")
print("=" * 70)
# The UI says TOOLS/IDENTITY/USER/HEARTBEAT/BOOTSTRAP/MEMORY are "MISSING"
# for tickles-n-co_schemy, but we just wrote all 9 files at
# /root/.openclaw/agents/tickles-n-co_schemy/*.md
# Let's hunt for other paths OpenClaw might look at.
candidates = [
    OC / "agents" / "tickles-n-co_schemy",
    OC / "tickles-n-co_schemy",
    OC / "workspaces" / "tickles-n-co_schemy",
    OC / "workspace" / "tickles-n-co_schemy",
    OC / "identities" / "tickles-n-co_schemy",
    OC / "overlays" / "tickles-n-co_schemy",
    OC / "configs" / "tickles-n-co_schemy",
]
for p in candidates:
    if p.exists():
        contents = sorted(x.name for x in p.iterdir())
        print(f"  EXISTS: {p}  -> {contents}")
    else:
        print(f"  (no)  : {p}")

print()
print("All dirs in /root/.openclaw:")
for d in sorted(OC.iterdir()):
    if d.is_dir():
        print(f"  {d.name}/")

print()
print("=" * 70)
print("Q2b. Does OpenClaw's UI code have a different expected filename?")
print("=" * 70)
# The UI uses lowercase tab names. Maybe it looks for lowercase files?
# Check if OpenClaw has its gateway code readable.
try:
    out = subprocess.run(
        ["find", "/opt/openclaw", "-type", "f", "-name", "*.js", "-path", "*gateway*"],
        capture_output=True, text=True, timeout=5,
    )
    print(out.stdout[:500] or "(no /opt/openclaw)")
except Exception as e:
    print(f"find failed: {e}")

# Maybe the binary is npm-installed elsewhere?
try:
    out = subprocess.run(["which", "openclaw-gateway"], capture_output=True, text=True)
    print("which openclaw-gateway:", out.stdout.strip() or out.stderr.strip())
except Exception as e:
    print(e)

try:
    out = subprocess.run(
        ["ls", "-la", "/usr/lib/node_modules/"],
        capture_output=True, text=True, timeout=5,
    )
    print("global npm modules:")
    for line in out.stdout.splitlines()[:40]:
        print(" ", line)
except Exception as e:
    print(e)

print()
print("=" * 70)
print("Q3. PAPERCLIP AGENTS TABLE — any duplicates for Tickles n Co?")
print("=" * 70)
try:
    out = subprocess.run(
        [
            "sudo", "-u", "postgres", "psql", "-d", "paperclip", "-A", "-F", "|",
            "-c",
            "SELECT id, url_key, name, adapter_type, "
            "adapter_config->>'agentId' as oc_id, created_at "
            "FROM agents "
            "WHERE company_id='1def5087-1267-4bfc-8c99-069685fff525' "
            "ORDER BY created_at;",
        ],
        capture_output=True, text=True, timeout=10,
    )
    if out.returncode != 0:
        # Try discovering the right DB name
        print("psql paperclip failed:", out.stderr[:300])
        print("\nDiscovering Paperclip DB name...")
        out2 = subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-Atc",
             "SELECT datname FROM pg_database WHERE datname LIKE 'paperclip%' "
             "OR datname LIKE '%paperclip%';"],
            capture_output=True, text=True, timeout=5,
        )
        print("candidates:", out2.stdout.strip() or "(none)")
    else:
        print(out.stdout)
except Exception as e:
    print("postgres query failed:", e)
