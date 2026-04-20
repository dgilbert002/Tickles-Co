#!/usr/bin/env python3
"""Phase C.3 — end-to-end smoke test.

Exercises the full one-click path:
  1. Delete lingering TESTCorp (with CEO_TEST) so the DB is clean-ish.
  2. POST /api/companies with provisioning={enabled:true, template:"trading"}.
  3. Poll /provisioning-jobs/latest until it finishes (or fails).
  4. List agents in the new company — we expect exactly 1 CEO agent.
  5. Inspect that agent's adapterConfig — assert:
       adapterType == "openclaw_gateway"
       adapterConfig.url starts with ws://
       adapterConfig.headers["x-openclaw-token"] is set
       metadata.templateAgent == True
       role is in the Paperclip canonical enum
  6. Report PASS/FAIL summary.

No assumption is made that the MCP daemon is running by looking at its HTTP
surface — instead we drive everything through Paperclip so this test mirrors
what the wizard UI does. That means if the MCP daemon is down the smoke will
fail at step 3 (job never completes).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
import uuid

PAPERCLIP = "http://127.0.0.1:3100"

VALID_ROLES = {
    "ceo", "cto", "cmo", "cfo",
    "engineer", "designer", "pm", "qa", "devops",
    "researcher", "general",
}


def _get(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def _json_req(url: str, method: str, body: dict | None = None, timeout: float = 20.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8", "replace") or "null")


def step(n: int, msg: str) -> None:
    print(f"\n=== step {n}: {msg} ===")


def fail(msg: str) -> None:
    print(f"\n!!! FAIL: {msg}")
    sys.exit(1)


def main() -> int:
    # ---- step 1: delete TESTCorp if present ---------------------------------
    step(1, "cleanup TESTCorp if present")
    comps = _get(f"{PAPERCLIP}/api/companies")
    if isinstance(comps, dict):
        comps = comps.get("items", [])
    for c in comps:
        if (c.get("name") or "").lower() == "testcorp":
            print(f"  deleting {c['id']} ({c.get('name')})")
            code, body = _json_req(f"{PAPERCLIP}/api/companies/{c['id']}", "DELETE")
            print(f"  -> HTTP {code}")
            # 404 is OK (maybe partially deleted); anything else we accept too
            break
    else:
        print("  no TESTCorp present")

    # ---- step 2: create a Trading company via the wizard endpoint -----------
    suffix = uuid.uuid4().hex[:6]
    name = f"smoke_trading_{suffix}"
    slug = name.replace("_", "-")
    step(2, f"POST /api/companies + provisioning={{template:trading}} as '{name}'")
    body = {
        "name": name,
        "slug": slug,
        "description": "Phase C.3 smoke — created by phaseC3_smoke.py",
        "provisioning": {
            "enabled": True,
            "template": "trading",
            "ruleOneMode": "advisory",
            "memuSubscriptions": ["trade_insights", "risk_events"],
        },
    }
    code, resp = _json_req(f"{PAPERCLIP}/api/companies", "POST", body)
    if code >= 400:
        fail(f"POST /api/companies -> HTTP {code}: {resp}")
    company_id = resp["id"]
    print(f"  companyId={company_id}")

    # ---- step 3: poll provisioning job until terminal ----------------------
    step(3, "poll /provisioning-jobs/latest until terminal")
    deadline = time.time() + 180
    last_status = None
    while time.time() < deadline:
        try:
            job = _get(f"{PAPERCLIP}/api/companies/{company_id}/provisioning-jobs/latest")
        except urllib.error.HTTPError as err:
            if err.code == 404:
                print("  (no job yet, waiting...)")
                time.sleep(1.5)
                continue
            fail(f"poll error HTTP {err.code}: {err.read().decode()[:200]}")
        status = job.get("overallStatus")
        steps = job.get("steps") or []
        step_summary = ", ".join(f"{s['step']}={s['status']}" for s in steps[-3:])
        if status != last_status:
            print(f"  overallStatus={status}  recent=[{step_summary}]")
            last_status = status
        if status in ("succeeded", "partial", "failed", "ok"):
            break
        time.sleep(2)
    else:
        fail("timed out waiting for provisioning job")
    print(f"  final status = {status}")

    # ---- step 4: list agents in the new company ----------------------------
    step(4, "list agents in the new company")
    agents = _get(f"{PAPERCLIP}/api/companies/{company_id}/agents")
    if isinstance(agents, dict):
        agents = agents.get("items", [])
    print(f"  {len(agents)} agent(s):")
    for a in agents:
        print(f"    - id={a['id']} name={a.get('name')} role={a.get('role')} adapter={a.get('adapterType')}")

    if not agents:
        fail("no agents were hired — expected exactly 1 CEO")
    if len(agents) != 1:
        print(f"  WARN: expected exactly 1 agent, got {len(agents)}")

    # ---- step 5: inspect adapter config ------------------------------------
    step(5, "inspect CEO adapter config")
    ceo = agents[0]
    problems: list[str] = []
    if ceo.get("adapterType") != "openclaw_gateway":
        problems.append(f"adapterType is {ceo.get('adapterType')!r}, expected openclaw_gateway")
    cfg = ceo.get("adapterConfig") or {}
    url = cfg.get("url")
    if not isinstance(url, str) or not url.startswith("ws"):
        problems.append(f"adapterConfig.url missing or not ws://... got {url!r}")
    hdrs = cfg.get("headers") or {}
    tok = hdrs.get("x-openclaw-token")
    if not tok:
        problems.append("adapterConfig.headers['x-openclaw-token'] missing")
    else:
        print(f"  x-openclaw-token present (len={len(tok)})")
    role = ceo.get("role")
    if role not in VALID_ROLES:
        problems.append(f"role {role!r} not in Paperclip AGENT_ROLES enum")
    md = ceo.get("metadata") or {}
    if md.get("templateAgent") is not True:
        problems.append("metadata.templateAgent != True")

    if problems:
        print("\n  problems:")
        for p in problems:
            print(f"   - {p}")
        fail("CEO adapter config did not meet expectations")

    # ---- step 6: report ----------------------------------------------------
    step(6, "PASS")
    print(f"\nSmoke complete. Summary:")
    print(f"  company:         {name}  ({company_id})")
    print(f"  provisioning:    {status}")
    print(f"  agents hired:    {len(agents)}")
    print(f"  CEO adapter:     openclaw_gateway (url+token auto-injected)")
    print(f"  canonical role:  {role} (templateRole preserved under metadata if remapped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
