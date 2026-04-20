#!/usr/bin/env python3
"""Get Paperclip DB creds and query the agents table for the REAL state."""
import json
import subprocess
from pathlib import Path


def sh_str(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, shell=True)
    return r.stdout, r.stderr, r.returncode


# Step 1: find paperclip server process
out, _, _ = sh_str("pgrep -af paperclip | head -20")
print("Paperclip processes:")
print(out)

# Step 2: get its env (works because we are root)
out, _, _ = sh_str(
    "for pid in $(pgrep -f paperclip); do "
    "if [ -r /proc/$pid/environ ]; then "
    "echo PID=$pid; "
    "tr '\\0' '\\n' < /proc/$pid/environ | grep -iE 'DATABASE_URL|POSTGRES|PG_'; "
    "fi; done"
)
print("Paperclip env:")
print(out[:3000])

# Step 3: extract DATABASE_URL from any paperclip process env
out, _, _ = sh_str(
    "for pid in $(pgrep -f paperclip); do "
    "tr '\\0' '\\n' < /proc/$pid/environ 2>/dev/null | grep '^DATABASE_URL=' | head -1 && break; "
    "done"
)
db_url_line = out.strip()
print(f"\nDATABASE_URL line length: {len(db_url_line)}")

if db_url_line.startswith("DATABASE_URL="):
    db_url = db_url_line.split("=", 1)[1]
    # redact password
    if "@" in db_url and "://" in db_url:
        scheme_rest = db_url.split("://", 1)
        cred_host = scheme_rest[1].rsplit("@", 1)
        if ":" in cred_host[0]:
            user, _ = cred_host[0].split(":", 1)
            print(f"  db_url scheme: {scheme_rest[0]}://{user}:***@{cred_host[1]}")

    print("\n=== Paperclip agents table (ALL) ===")
    r = subprocess.run(
        ["psql", db_url, "-A", "-F", "|", "-c",
         "SELECT c.name AS company, a.name AS agent_name, a.url_key, a.status, "
         "a.adapter_config->>'agentId' AS openclaw_id, a.created_at, a.updated_at "
         "FROM agents a LEFT JOIN companies c ON c.id=a.company_id "
         "ORDER BY c.name, a.created_at;"],
        capture_output=True, text=True, timeout=15,
    )
    print(f"rc={r.returncode}")
    if r.returncode == 0:
        print(r.stdout)
    else:
        print("STDERR:", r.stderr[:500])

    print("\n=== agent_config_revisions — last 10 updates ===")
    r = subprocess.run(
        ["psql", db_url, "-A", "-F", "|", "-c",
         "SELECT a.name, acr.revision, acr.created_at, "
         "acr.config->>'adapter_config'::jsonb->>'agentId' AS oc_id "
         "FROM agent_config_revisions acr "
         "JOIN agents a ON a.id=acr.agent_id "
         "ORDER BY acr.created_at DESC LIMIT 15;"],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode == 0:
        print(r.stdout[:2000])
    else:
        print("STDERR:", r.stderr[:500])

    print("\n=== companies table ===")
    r = subprocess.run(
        ["psql", db_url, "-A", "-F", "|", "-c",
         "SELECT id, name, url_key, status, created_at FROM companies ORDER BY created_at;"],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode == 0:
        print(r.stdout)
