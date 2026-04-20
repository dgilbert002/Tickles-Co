#!/usr/bin/env python3
"""Phase C.3 — verify the already-provisioned smoke company.

Picks up where phaseC3_smoke.py left off (its first run timed out on terminal
status detection but the provisioning DID complete — status=ok).
"""
from __future__ import annotations

import json
import sys
import urllib.request

PAPERCLIP = "http://127.0.0.1:3100"
VALID_ROLES = {
    "ceo", "cto", "cmo", "cfo",
    "engineer", "designer", "pm", "qa", "devops",
    "researcher", "general",
}


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.load(resp)


def main() -> int:
    print("=== listing all companies to find smoke_trading_* ===")
    comps = _get(f"{PAPERCLIP}/api/companies")
    if isinstance(comps, dict):
        comps = comps.get("items", [])
    smokes = [c for c in comps if (c.get("name") or "").startswith("smoke_trading_")]
    if not smokes:
        print("FAIL: no smoke_trading_* company found")
        return 1
    # Use the most recent (largest id / last in list)
    comp = smokes[-1]
    company_id = comp["id"]
    print(f"company: {comp['name']} ({company_id})")

    print("\n=== provisioning job ===")
    job = _get(f"{PAPERCLIP}/api/companies/{company_id}/provisioning-jobs/latest")
    print(f"  overallStatus = {job.get('overallStatus')}")
    print(f"  templateId    = {job.get('templateId')}")
    print(f"  steps         = {len(job.get('steps') or [])}")
    for s in job.get("steps") or []:
        print(f"    [{s.get('stepIndex'):>2}] {s.get('step'):<25} "
              f"{s.get('status'):<10} {(s.get('detail') or '')[:80]}")

    print("\n=== agents ===")
    agents = _get(f"{PAPERCLIP}/api/companies/{company_id}/agents")
    if isinstance(agents, dict):
        agents = agents.get("items", [])
    print(f"  {len(agents)} agent(s)")

    problems = []
    for a in agents:
        cfg = a.get("adapterConfig") or {}
        hdrs = cfg.get("headers") or {}
        url = cfg.get("url")
        tok = hdrs.get("x-openclaw-token")
        role = a.get("role")
        md = a.get("metadata") or {}
        adapter = a.get("adapterType")
        print(f"  - {a.get('name'):<10} role={role:<12} adapter={adapter:<18} "
              f"url={'y' if url else 'n'} token={'y' if tok else 'n'} "
              f"templateAgent={md.get('templateAgent')} "
              f"templateRole={md.get('templateRole')}")
        if adapter != "openclaw_gateway":
            problems.append(f"{a.get('name')}: wrong adapter={adapter}")
        if not url or not url.startswith("ws"):
            problems.append(f"{a.get('name')}: url missing")
        if not tok:
            problems.append(f"{a.get('name')}: x-openclaw-token missing")
        if role not in VALID_ROLES:
            problems.append(f"{a.get('name')}: invalid role {role}")
        if md.get("templateAgent") is not True:
            problems.append(f"{a.get('name')}: metadata.templateAgent != True")

    if problems:
        print("\n=== PROBLEMS ===")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\n=== PASS ===")
    print("Every hired agent has:")
    print("  * adapterType == openclaw_gateway (underscore)")
    print("  * adapterConfig.url auto-injected")
    print("  * adapterConfig.headers['x-openclaw-token'] auto-injected")
    print("  * role mapped to a Paperclip canonical enum value")
    print("  * metadata.templateAgent marker")
    return 0


if __name__ == "__main__":
    sys.exit(main())
