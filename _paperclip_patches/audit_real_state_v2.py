#!/usr/bin/env python3
"""Deeper audit: Paperclip DB creds + full agents+companies listing."""
import json
import subprocess
from pathlib import Path


def sh(cmd, timeout=10):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.stderr, r.returncode


print("A. Paperclip env file content (sanitized)")
envf = Path("/home/paperclip/.paperclip/instances/default/.env")
if envf.exists():
    for ln in envf.read_text().splitlines():
        if any(k in ln.upper() for k in ("PASSWORD", "SECRET", "TOKEN", "KEY")):
            k = ln.split("=", 1)[0] if "=" in ln else ln
            print(f"  {k}=<redacted>")
        else:
            print(f"  {ln}")

print()
print("B. All Paperclip agent directories (both companies, all status)")
companies = Path("/home/paperclip/.paperclip/instances/default/companies")
for cdir in sorted(companies.iterdir()):
    if not cdir.is_dir():
        continue
    cid = cdir.name
    print(f"\n  company: {cid}")
    for sub in sorted(cdir.iterdir()):
        print(f"    {sub.name}/")
    agents_dir = cdir / "agents"
    if agents_dir.exists():
        for adir in sorted(agents_dir.iterdir()):
            if not adir.is_dir():
                continue
            aid = adir.name
            instr = adir / "instructions"
            files = []
            if instr.exists():
                files = [p.name for p in instr.iterdir()]
            print(f"      agent: {aid}  instructions: {files}")
            # Peek inside AGENTS.md to learn which agent this is
            agents_md = instr / "AGENTS.md"
            if agents_md.exists():
                try:
                    first_lines = agents_md.read_text()[:300]
                    print(f"        HEAD: {first_lines[:240]!r}")
                except Exception:
                    pass

print()
print("C. Query Paperclip DB via 127.0.0.1:54329 — need password")
# discover Paperclip DB password
for candidate in [
    Path("/home/paperclip/.paperclip/instances/default/postgres.env"),
    Path("/home/paperclip/.paperclip/instances/default/.env"),
    Path("/etc/paperclip/postgres.env"),
]:
    if candidate.exists():
        print(f"  trying: {candidate}")
        for ln in candidate.read_text().splitlines():
            if "PGPASSWORD" in ln.upper() or "POSTGRES_PASSWORD" in ln.upper():
                print(f"    found pw-bearing line (not shown)")
                break

# Check paperclip process env for the actual connection string
out, _, _ = sh(["bash", "-c",
                "cat /proc/$(pgrep -f 'paperclip.*server' | head -1)/environ 2>/dev/null | tr '\\0' '\\n' | grep -iE 'DATABASE|POSTGRES|PG_'"])
if out.strip():
    print("  paperclip server env:")
    for ln in out.strip().splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            # redact password component
            if "://" in v and "@" in v:
                scheme, rest = v.split("://", 1)
                if "@" in rest:
                    cred, host = rest.rsplit("@", 1)
                    if ":" in cred:
                        user, _pw = cred.split(":", 1)
                        v = f"{scheme}://{user}:[REDACTED]@{host}"
            print(f"    {k}=<{len(v)} chars>  (scheme={v[:40] if len(v) < 100 else v[:40]+'...'})")

print()
print("D. Now query with env connection string")
out, _, _ = sh(["bash", "-c",
                "tr '\\0' '\\n' < /proc/$(pgrep -f 'paperclip.*server' | head -1)/environ 2>/dev/null | grep '^DATABASE_URL=' | head -1"])
db_url = out.strip().split("=", 1)[1] if "=" in out else ""
if db_url:
    print(f"  found DATABASE_URL (length={len(db_url)})")
    r = subprocess.run(
        ["psql", db_url, "-A", "-F", "|", "-c",
         "SELECT c.name, a.id, a.name, a.url_key, a.adapter_type, "
         "a.adapter_config->>'agentId' AS oc_id, a.status, a.created_at "
         "FROM agents a LEFT JOIN companies c ON c.id=a.company_id "
         "ORDER BY c.name, a.created_at;"],
        capture_output=True, text=True, timeout=15,
    )
    print(f"  rc={r.returncode}")
    print(r.stdout[:4000])
    if r.stderr:
        print("  STDERR:", r.stderr[:500])
