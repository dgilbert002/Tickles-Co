#!/usr/bin/env python3
"""Phase 5 backfill — bring every existing agent up to the Phase-5 standard.

For each agent in Paperclip whose adapterType=openclaw_gateway this script:

  1. Computes a stable global key: <companySlug>_<urlKey>.
  2. PATCHes the agent so `adapterConfig.agentId` = that key (OpenClaw's
     gateway looks up the folder by this field).
  3. Clones `/root/.openclaw/agents/cody` -> `/root/.openclaw/agents/<key>/`
     when the folder doesn't exist.
  4. Writes the full Phase-5 overlay set via the executor's canonical
     `_openclaw_customize(...)` (8 markdown overlays + meta.json).
  5. Upserts the agent into `/root/.openclaw/openclaw.json agents.list[]` via
     the executor's `_openclaw_register_in_registry(...)` so the OpenClaw
     GUI dropdown can see it.

Rollback: every openclaw.json mutation drops a `.bak.phase5-<ISO>` next to
the file before writing. `cp openclaw.json.bak.phase5-<ISO> openclaw.json`
undoes the registry changes. The overlay markdown files carry a
`<!-- generated-by -->` header so future re-runs can skip hand-edited files.

Reuses executor code so behaviour is identical to a fresh `hire_agents` call.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from typing import Any, Dict, List

sys.path.insert(0, "/opt/tickles")

from shared.provisioning.executor import (  # type: ignore
    OPENCLAW_AGENTS_DIR,
    _openclaw_clone,
    _openclaw_customize,
    _openclaw_register_in_registry,
)
from shared.provisioning.templates import TemplateAgent  # type: ignore


PAPERCLIP = os.environ.get("PAPERCLIP_URL", "http://127.0.0.1:3100")
TEMPLATE_SOURCE = "cody"
DEFAULT_MODEL = "openrouter/anthropic/claude-sonnet-4"
# None = "all companies". Set to a specific set to limit.
KEEP_COMPANIES: set[str] | None = None


def http(method: str, path: str, body: Any = None) -> tuple[int | None, Any]:
    url = f"{PAPERCLIP}{path}"
    data = None
    headers = {"content-type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw
        return e.code, parsed


def companies() -> List[Dict[str, Any]]:
    status, data = http("GET", "/api/companies")
    if status != 200:
        print(f"[backfill] GET /api/companies -> {status}")
        sys.exit(1)
    if isinstance(data, dict):
        data = data.get("companies") or data.get("data") or []
    return data


def agents(company_id: str) -> List[Dict[str, Any]]:
    status, data = http("GET", f"/api/companies/{company_id}/agents")
    if status != 200:
        return []
    if isinstance(data, dict):
        data = data.get("agents") or data.get("data") or []
    return data


def infer_skills(company_slug: str, role: str) -> List[str]:
    """Derive plausible skill names for an agent when the template is lost.

    Tickles n Co agents = observers, Building agents = core, Trading company
    agents = trading skills. Purely advisory — the authoritative tool surface
    is the live MCP `tools/list`.
    """
    role_l = (role or "").lower()
    if company_slug == "tickles-n-co":
        if "code" in role_l or "engineer" in role_l:
            return ["code-scan", "git-status"]
        if "db" in role_l or "schemy" in role_l or "observer" in role_l:
            return ["db-introspect", "schema-diff"]
        if "audit" in role_l or "qa" in role_l:
            return ["audit", "log-review"]
        return []
    if company_slug == "building":
        if "janitor" in role_l:
            return ["cleanup", "db-introspect", "log-review"]
        if "strategy" in role_l or "council" in role_l:
            return ["memu-broadcast", "memu-search", "governance"]
        return ["governance", "memu-search"]
    # Default for trading companies.
    return ["ccxt-pro", "indicator-library", "backtest-submit"]


def synth_template_agent(
    *,
    name: str,
    role: str,
    url_key: str,
    soul: str,
    model: str,
    skills: List[str],
    budget_monthly_cents: int,
) -> TemplateAgent:
    """Rebuild a TemplateAgent struct from a live Paperclip agent row so we
    can feed the executor's customize/register helpers."""
    return TemplateAgent(
        url_key=url_key,
        name=name,
        role=role,
        model=model,
        soul=soul,
        skills=skills,
        budget_monthly_cents=budget_monthly_cents,
        clone_openclaw_from=TEMPLATE_SOURCE,
    )


def process_agent(
    *, company: Dict[str, Any], slug: str, a: Dict[str, Any]
) -> None:
    aid = a.get("id")
    aname = a.get("name") or "?"
    urlkey = (a.get("urlKey") or aname).lower()
    role = a.get("role") or "general"
    cfg = dict(a.get("adapterConfig") or {})
    runtime_cfg = a.get("runtimeConfig") or {}
    soul = runtime_cfg.get("soul") or ""
    model = cfg.get("model") or DEFAULT_MODEL
    budget = int(a.get("budgetMonthlyCents") or 20000)

    skills_meta = (a.get("metadata") or {}).get("skills") or []
    skills = list(skills_meta) if skills_meta else infer_skills(slug, role)

    global_key = f"{slug}_{urlkey}"
    print(f"  - {aname!r} urlKey={urlkey!r} currentAgentId={cfg.get('agentId')!r}")

    tpl = synth_template_agent(
        name=aname,
        role=role,
        url_key=urlkey,
        soul=soul,
        model=model,
        skills=skills,
        budget_monthly_cents=budget,
    )

    openclaw_dir = _openclaw_clone(TEMPLATE_SOURCE, global_key)
    if not openclaw_dir:
        print("    !! could not clone openclaw folder (template missing?)")
        return

    overlay = _openclaw_customize(
        openclaw_dir,
        agent=tpl,
        slug=slug,
        company_id=company["id"],
        global_url_key=global_key,
        force_overwrite=True,
    )
    print(f"    * overlays written={len(overlay.get('written', []))} preserved={len(overlay.get('preserved', []))}")

    registry = _openclaw_register_in_registry(
        global_url_key=global_key,
        agent=tpl,
        slug=slug,
        heartbeat_every=None,
    )
    if registry.get("ok"):
        print(f"    * openclaw.json {'replaced' if registry.get('replaced') else 'appended'} id={global_key}")
    else:
        print(f"    !! openclaw.json upsert failed: {registry.get('error')}")

    if cfg.get("agentId") != global_key:
        patch_cfg = dict(cfg)
        patch_cfg["agentId"] = global_key
        patch_cfg.setdefault("agentKey", global_key)
        status, resp = http(
            "PATCH", f"/api/agents/{aid}", body={"adapterConfig": patch_cfg}
        )
        if 200 <= (status or 0) < 300:
            print(f"    + PATCHed adapterConfig.agentId = {global_key!r}")
        else:
            print(f"    !! PATCH failed http={status} body={resp}")
    else:
        print(f"    = agentId already correct ({global_key})")


def main() -> None:
    for c in companies():
        name = c.get("name")
        if KEEP_COMPANIES is not None and name not in KEEP_COMPANIES:
            continue
        slug = (c.get("slug") or c.get("urlKey") or name or "").lower().replace(" ", "-")
        print(f"\n== {name} (id={c['id']}, slug={slug}) ==")
        for a in agents(c["id"]):
            if a.get("adapterType") != "openclaw_gateway":
                continue
            try:
                process_agent(company=c, slug=slug, a=a)
            except Exception as exc:
                print(f"    !! exception on agent {a.get('name')}: {exc}")


if __name__ == "__main__":
    main()
