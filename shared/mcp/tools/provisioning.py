"""MCP tools: Provisioning (company + agent lifecycle).

Every handler here is a thin facade over Paperclip's HTTP API. Paperclip is
the source of truth for companies, agents, budgets and runtime config; the
MCP just exposes these over the tool protocol so LLM agents (via OpenClaw or
Paperclip adapters) can manage the fleet conversationally.

Tools registered:
    company.list
    company.get
    company.create       (Paperclip row; optional chained provisioning)
    company.delete
    company.pause          (set status=paused)
    company.resume         (set status=active)
    company.templates      (list available provisioning templates)    ← Phase-3
    company.provision      (run 9-step executor against existing row) ← Phase-3
    agent.list
    agent.get
    agent.create
    agent.delete
    agent.pause
    agent.resume
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from ..protocol import McpTool
from ..registry import ToolRegistry
from .context import ToolContext

LOG = logging.getLogger("tickles.mcp.tools.provisioning")

# Phase-3 — 9-step executor lives in shared.provisioning. Import is lazy inside
# the handler so tools/list stays fast even when the executor has unmet deps
# (psql, qdrant) at tool-registration time.


# ---- schema fragments -------------------------------------------------

_COMPANY_CREATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "issuePrefix": {
            "type": "string",
            "description": "3-4 letter ticket prefix, e.g. SUR, POL",
            "minLength": 2,
            "maxLength": 6,
        },
        "description": {"type": "string"},
        "budgetMonthlyCents": {"type": "integer", "minimum": 0},
        "requireBoardApprovalForNewAgents": {"type": "boolean"},
        "brandColor": {"type": "string"},
        # Phase-3 — optional chained provisioning. When `enabled=true` the
        # handler creates the Paperclip row, then runs the 9-step executor
        # against it. When absent or `enabled=false` behaviour is unchanged
        # from Phase 2 (Paperclip row only).
        "provisioning": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "default": False},
                "template": {
                    "type": "string",
                    "description": "Template id, e.g. blank, media, surgeon_co",
                },
                "slug": {
                    "type": "string",
                    "description": "Override auto-slug (alphanumeric + underscore)",
                },
                "jobId": {
                    "type": "string",
                    "description": (
                        "Optional Paperclip `company_provisioning_jobs.id` to "
                        "stream step events against. When omitted, executor "
                        "runs without UI progress reporting."
                    ),
                },
            },
            "required": ["enabled"],
        },
    },
    "required": ["name"],
}

_COMPANY_PROVISION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "companyId": {"type": "string"},
        "slug": {"type": "string"},
        "templateId": {"type": "string"},
        "jobId": {
            "type": "string",
            "description": (
                "Optional Paperclip `company_provisioning_jobs.id` to stream "
                "per-step events to. Paperclip's POST /api/companies handler "
                "creates this row before calling us; CLI invocations can omit."
            ),
        },
    },
    "required": ["companyId", "templateId"],
}

_AGENT_CREATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "companyId": {"type": "string"},
        "name": {"type": "string", "minLength": 1},
        "title": {"type": "string"},
        "role": {"type": "string", "default": "general"},
        "adapterType": {"type": "string", "default": "openclaw_gateway"},
        "adapterConfig": {
            "type": "object",
            "description": "Raw adapter config passed through to Paperclip",
        },
        "budgetMonthlyCents": {"type": "integer", "minimum": 0},
        "heartbeatIntervalSec": {"type": "integer", "minimum": 60},
        "reportsTo": {"type": "string"},
    },
    "required": ["companyId", "name"],
}


# ---- handlers ---------------------------------------------------------


def _build_tools(ctx: ToolContext) -> list[tuple[McpTool, Any]]:
    # company.list
    t_company_list = McpTool(
        name="company.list",
        description=(
            "List all companies registered in Paperclip (id, name, status, "
            "monthly budget, agent count). Read-only."
        ),
        version="1",
        input_schema={"type": "object", "properties": {}},
        read_only=True,
        tags={"phase": "2", "group": "provisioning"},
    )

    async def _company_list(_: Dict[str, Any]) -> Dict[str, Any]:
        rows = ctx.paperclip("GET", "/api/companies") or []
        return {"count": len(rows), "companies": rows}

    # company.get
    t_company_get = McpTool(
        name="company.get",
        description="Fetch a single company by id.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {"companyId": {"type": "string"}},
            "required": ["companyId"],
        },
        read_only=True,
        tags={"phase": "2", "group": "provisioning"},
    )

    async def _company_get(p: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(p["companyId"])
        return ctx.paperclip("GET", f"/api/companies/{cid}")

    # company.create
    t_company_create = McpTool(
        name="company.create",
        description=(
            "Create a new company in Paperclip. Returns the created row. "
            "When `provisioning.enabled=true`, also runs the 9-step Phase-3 "
            "executor (database, Qdrant, mem0 scopes, MemU subscriptions, "
            "Treasury, agents, routines) against the newly-created row."
        ),
        version="2",
        input_schema=_COMPANY_CREATE_SCHEMA,
        read_only=False,
        tags={"phase": "3", "group": "provisioning"},
    )

    async def _company_create(p: Dict[str, Any]) -> Dict[str, Any]:
        # 1) create the Paperclip row (always). Strip our extra key before POSTing.
        prov = p.get("provisioning") or {}
        body = {k: v for k, v in p.items() if v is not None and k != "provisioning"}
        created = ctx.paperclip("POST", "/api/companies", body=body)
        if not prov.get("enabled"):
            return created

        # 2) chain into provisioning. Late import so the MCP daemon can start
        #    even when shared.provisioning has unmet runtime deps (e.g. no
        #    psql on PATH) — you just can't call this branch then.
        from shared.provisioning import executor as _exec  # noqa: WPS433

        template_id = prov.get("template") or "blank"
        slug = prov.get("slug") or created.get("issuePrefix") or created.get("name", "company")
        job_id = prov.get("jobId")

        LOG.info(
            "[company.create] chaining provisioning company_id=%s template=%s slug=%s job_id=%s",
            created.get("id"), template_id, slug, job_id,
        )
        try:
            result = await _exec.run(
                company_id=str(created["id"]),
                slug=str(slug),
                template_id=str(template_id),
                job_id=str(job_id) if job_id else None,
            )
            return {
                "company": created,
                "provisioning": result.to_payload(),
            }
        except Exception as err:  # pragma: no cover
            LOG.exception("[company.create] provisioning threw")
            return {
                "company": created,
                "provisioning": {
                    "overallStatus": "failed",
                    "error": str(err),
                    "steps": [],
                },
            }

    # company.templates — Phase-3 — list available templates for UI dropdown
    t_company_templates = McpTool(
        name="company.templates",
        description=(
            "List every company template under shared/templates/companies/*.json. "
            "Used by the Paperclip create-company modal to populate the template "
            "dropdown. Read-only."
        ),
        version="1",
        input_schema={"type": "object", "properties": {}},
        read_only=True,
        tags={"phase": "3", "group": "provisioning"},
    )

    async def _company_templates(_: Dict[str, Any]) -> Dict[str, Any]:
        from shared.provisioning import list_available  # noqa: WPS433
        tpls = list_available()
        return {
            "count": len(tpls),
            "templates": [t.to_public_dict() for t in tpls],
        }

    # company.provision — Phase-3 — run executor against an existing company
    t_company_provision = McpTool(
        name="company.provision",
        description=(
            "Run the 9-step provisioning executor against an existing Paperclip "
            "company. Idempotent: re-running on an already-provisioned company "
            "returns `ok` with each step reporting it was already present."
        ),
        version="1",
        input_schema=_COMPANY_PROVISION_SCHEMA,
        read_only=False,
        tags={"phase": "3", "group": "provisioning"},
    )

    async def _company_provision(p: Dict[str, Any]) -> Dict[str, Any]:
        from shared.provisioning import executor as _exec  # noqa: WPS433
        company_id = str(p["companyId"])
        template_id = str(p["templateId"])
        slug = str(p.get("slug") or "")
        if not slug:
            # derive from Paperclip row
            row = ctx.paperclip("GET", f"/api/companies/{company_id}")
            slug = (row or {}).get("issuePrefix") or (row or {}).get("name", company_id)
        job_id = p.get("jobId")
        LOG.info(
            "[company.provision] company_id=%s template=%s slug=%s job_id=%s",
            company_id, template_id, slug, job_id,
        )
        result = await _exec.run(
            company_id=company_id,
            slug=str(slug),
            template_id=template_id,
            job_id=str(job_id) if job_id else None,
        )
        return result.to_payload()

    # company.delete
    t_company_delete = McpTool(
        name="company.delete",
        description=(
            "Soft-delete a company (status=archived). All agents are paused."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {"companyId": {"type": "string"}},
            "required": ["companyId"],
        },
        read_only=False,
        tags={"phase": "2", "group": "provisioning", "destructive": True},
    )

    async def _company_delete(p: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(p["companyId"])
        return ctx.paperclip("DELETE", f"/api/companies/{cid}") or {"ok": True}

    # company.pause / company.resume
    async def _company_status(cid: str, status: str) -> Dict[str, Any]:
        return ctx.paperclip(
            "PATCH", f"/api/companies/{cid}", body={"status": status}
        )

    t_company_pause = McpTool(
        name="company.pause",
        description="Pause a company (all agents stop taking new heartbeats).",
        version="1",
        input_schema={
            "type": "object",
            "properties": {"companyId": {"type": "string"}},
            "required": ["companyId"],
        },
        read_only=False,
        tags={"phase": "2", "group": "provisioning"},
    )

    async def _company_pause(p: Dict[str, Any]) -> Dict[str, Any]:
        return await _company_status(str(p["companyId"]), "paused")

    t_company_resume = McpTool(
        name="company.resume",
        description="Resume a paused company.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {"companyId": {"type": "string"}},
            "required": ["companyId"],
        },
        read_only=False,
        tags={"phase": "2", "group": "provisioning"},
    )

    async def _company_resume(p: Dict[str, Any]) -> Dict[str, Any]:
        return await _company_status(str(p["companyId"]), "active")

    # agent.list
    t_agent_list = McpTool(
        name="agent.list",
        description="List agents in a company (with last heartbeat + status).",
        version="1",
        input_schema={
            "type": "object",
            "properties": {"companyId": {"type": "string"}},
            "required": ["companyId"],
        },
        read_only=True,
        tags={"phase": "2", "group": "provisioning"},
    )

    async def _agent_list(p: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(p["companyId"])
        rows = ctx.paperclip("GET", f"/api/companies/{cid}/agents") or []
        return {"companyId": cid, "count": len(rows), "agents": rows}

    # agent.get
    t_agent_get = McpTool(
        name="agent.get",
        description="Fetch a single agent by id.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {"type": "string"},
                "agentId": {"type": "string"},
            },
            "required": ["companyId", "agentId"],
        },
        read_only=True,
        tags={"phase": "2", "group": "provisioning"},
    )

    async def _agent_get(p: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(p["companyId"])
        aid = str(p["agentId"])
        return ctx.paperclip("GET", f"/api/companies/{cid}/agents/{aid}")

    # agent.create
    t_agent_create = McpTool(
        name="agent.create",
        description=(
            "Create an agent inside a company. The Phase-3 wizard will fill in "
            "AGENTS.md/SOUL.md from the company template; this tool is the low-"
            "level primitive the wizard calls."
        ),
        version="1",
        input_schema=_AGENT_CREATE_SCHEMA,
        read_only=False,
        tags={"phase": "2", "group": "provisioning"},
    )

    async def _agent_create(p: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(p["companyId"])
        body: Dict[str, Any] = {
            "name": p["name"],
            "role": p.get("role", "general"),
            "adapterType": p.get("adapterType", "openclaw_gateway"),
        }
        for key in ("title", "reportsTo", "budgetMonthlyCents", "adapterConfig"):
            if key in p and p[key] is not None:
                body[key] = p[key]
        if "heartbeatIntervalSec" in p and p["heartbeatIntervalSec"] is not None:
            body["runtimeConfig"] = {
                "heartbeat": {
                    "enabled": True,
                    "intervalSec": int(p["heartbeatIntervalSec"]),
                }
            }
        return ctx.paperclip("POST", f"/api/companies/{cid}/agents", body=body)

    # agent.delete
    t_agent_delete = McpTool(
        name="agent.delete",
        description="Delete an agent (sets status=archived).",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {"type": "string"},
                "agentId": {"type": "string"},
            },
            "required": ["companyId", "agentId"],
        },
        read_only=False,
        tags={"phase": "2", "group": "provisioning", "destructive": True},
    )

    async def _agent_delete(p: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(p["companyId"])
        aid = str(p["agentId"])
        return ctx.paperclip(
            "DELETE", f"/api/companies/{cid}/agents/{aid}"
        ) or {"ok": True}

    # agent.pause / agent.resume
    async def _agent_status(cid: str, aid: str, status: str) -> Dict[str, Any]:
        return ctx.paperclip(
            "PATCH",
            f"/api/companies/{cid}/agents/{aid}",
            body={"status": status},
        )

    t_agent_pause = McpTool(
        name="agent.pause",
        description="Pause an agent (skips scheduled heartbeats).",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {"type": "string"},
                "agentId": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["companyId", "agentId"],
        },
        read_only=False,
        tags={"phase": "2", "group": "provisioning"},
    )

    async def _agent_pause(p: Dict[str, Any]) -> Dict[str, Any]:
        return await _agent_status(
            str(p["companyId"]), str(p["agentId"]), "paused"
        )

    t_agent_resume = McpTool(
        name="agent.resume",
        description="Resume a paused agent.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {"type": "string"},
                "agentId": {"type": "string"},
            },
            "required": ["companyId", "agentId"],
        },
        read_only=False,
        tags={"phase": "2", "group": "provisioning"},
    )

    async def _agent_resume(p: Dict[str, Any]) -> Dict[str, Any]:
        return await _agent_status(
            str(p["companyId"]), str(p["agentId"]), "idle"
        )

    return [
        (t_company_list, _company_list),
        (t_company_get, _company_get),
        (t_company_create, _company_create),
        (t_company_delete, _company_delete),
        (t_company_pause, _company_pause),
        (t_company_resume, _company_resume),
        (t_company_templates, _company_templates),      # Phase 3
        (t_company_provision, _company_provision),      # Phase 3
        (t_agent_list, _agent_list),
        (t_agent_get, _agent_get),
        (t_agent_create, _agent_create),
        (t_agent_delete, _agent_delete),
        (t_agent_pause, _agent_pause),
        (t_agent_resume, _agent_resume),
    ]


def register(registry: ToolRegistry, ctx: ToolContext) -> None:
    for tool, handler in _build_tools(ctx):
        registry.register(tool, handler)
