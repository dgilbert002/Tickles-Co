#!/usr/bin/env python3
"""Query the REAL state of agents: Paperclip DB + on-disk SOULs + OpenClaw registry."""
import json
import os
import subprocess
from pathlib import Path


def sh(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=isinstance(cmd, str))
        return r.stdout, r.stderr, r.returncode
    except Exception as e:
        return "", str(e), 99


print("=" * 70)
print("A. Paperclip env + postgres socket")
print("=" * 70)
for f in ["/etc/paperclip/paperclip.env", "/etc/paperclip/openclaw-gateway.env"]:
    if Path(f).exists():
        try:
            content = Path(f).read_text()
            for line in content.splitlines():
                if any(k in line.upper() for k in ("DATABASE_URL", "POSTGRES", "PG_", "DB_")):
                    if any(c in line for c in ("=", ":")):
                        parts = line.split("=", 1)
                        if len(parts) == 2 and ":" in parts[1]:
                            parts[1] = parts[1].split(":")[0] + ":[REDACTED]"
                        print(f"  {f}: {parts[0]}=<{len(parts[1]) if len(parts)>1 else 0} chars>")
        except Exception as e:
            print(f"  {f}: read err {e}")
out, _, _ = sh(["ss", "-tlnp"])
for line in out.splitlines():
    if "5432" in line or "543" in line.split()[3] if len(line.split()) > 3 else False:
        if "543" in line:
            print("  socket:", line.strip()[:200])

print()
print("=" * 70)
print("B. Paperclip instances / companies on disk")
print("=" * 70)
inst = Path("/home/paperclip/.paperclip/instances/default")
if inst.exists():
    print(f"  instance dir: {inst}")
    companies_dir = inst / "companies"
    if companies_dir.exists():
        for cdir in sorted(companies_dir.iterdir()):
            if not cdir.is_dir():
                continue
            cid = cdir.name
            print(f"  company: {cid}")
            agents_dir = cdir / "agents"
            if agents_dir.exists():
                for adir in sorted(agents_dir.iterdir()):
                    if not adir.is_dir():
                        continue
                    aid = adir.name
                    instr = adir / "instructions" / "AGENTS.md"
                    if instr.exists():
                        sz = instr.stat().st_size
                        print(f"    agent: {aid}  instructions/AGENTS.md ({sz}b)")
                    else:
                        print(f"    agent: {aid}  (no instructions/AGENTS.md)")

print()
print("=" * 70)
print("C. Paperclip Postgres — discover port + creds")
print("=" * 70)
# Try common Paperclip port first
out, _, _ = sh(["sudo", "ss", "-tlnp"])
for line in out.splitlines():
    if any(p in line for p in ("5432", "54320", "54321", "54329")):
        print(" ", line.strip()[:200])

# Look for .paperclip env
pcenv = Path("/home/paperclip/.paperclip/instances/default/.env")
if pcenv.exists():
    print(f"  env file: {pcenv}")
    try:
        for line in pcenv.read_text().splitlines():
            if any(k in line.upper() for k in ("DATABASE", "POSTGRES", "PG_", "DB_")):
                print(f"    {line.split('=')[0]}=<set>")
    except Exception:
        pass

# Postgres data dir
pgdata = inst / "postgres"
if pgdata.exists():
    print(f"  pgdata exists: {pgdata}")
    if (pgdata / "postmaster.pid").exists():
        port_line = (pgdata / "postmaster.pid").read_text().splitlines()[3] if len(
            (pgdata / "postmaster.pid").read_text().splitlines()
        ) > 3 else ""
        print(f"  postmaster.pid line4 (port): {port_line}")

print()
print("=" * 70)
print("D. Query Paperclip agents table")
print("=" * 70)
# Try a few auth strategies to hit Paperclip DB
queries = [
    ("local as paperclip", ["sudo", "-u", "paperclip", "psql", "-p", "54329",
                             "-d", "paperclip", "-A", "-F", "|", "-c",
                             "SELECT c.name, a.id, a.name, a.url_key, a.adapter_type, "
                             "a.adapter_config->>'agentId' AS oc_id, a.created_at "
                             "FROM agents a LEFT JOIN companies c ON c.id=a.company_id "
                             "ORDER BY c.name, a.created_at;"]),
    ("local via socket", ["sudo", "-u", "paperclip", "psql",
                           "-h", "/home/paperclip/.paperclip/instances/default/postgres",
                           "-d", "paperclip", "-A", "-F", "|", "-c",
                           "SELECT c.name, a.id, a.name, a.url_key, a.adapter_type, "
                           "a.adapter_config->>'agentId' AS oc_id, a.created_at "
                           "FROM agents a LEFT JOIN companies c ON c.id=a.company_id "
                           "ORDER BY c.name, a.created_at;"]),
]
for label, cmd in queries:
    out, err, rc = sh(cmd, timeout=15)
    print(f"  [{label}] rc={rc}")
    if rc == 0:
        print(out[:2500])
        break
    else:
        print(f"    err: {err.strip()[:250]}")

print()
print("=" * 70)
print("E. OpenClaw agents.list ids (from openclaw.json)")
print("=" * 70)
cfg = Path("/root/.openclaw/openclaw.json")
if cfg.exists():
    try:
        d = json.loads(cfg.read_text())
        lst = d.get("agents", {}).get("list", [])
        for a in lst:
            if isinstance(a, dict):
                print(f"  id={a.get('id'):<45} hb={a.get('heartbeat', {}).get('every', '-')}")
    except Exception as e:
        print(f"  read err: {e}")
