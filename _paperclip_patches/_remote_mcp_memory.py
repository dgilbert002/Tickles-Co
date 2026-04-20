"""MCP tools: Memory (Tier 1 mem0, Tier 2 shared, Tier 3 MemU).

The three-tier memory architecture (see ``.cursor/rules/tickles-co-architecture.mdc``):

* Tier 1 — agent-private mem0/Qdrant, scoped per-agent.
* Tier 2 — company-shared mem0/Qdrant, scoped per-company + SQL table.
* Tier 3 — cross-company MemU (Postgres + pgvector + pg_notify broadcast).

Phase 2 exposes the MCP surface. Tier 1/2 calls go through the user-mem0 MCP
which is already running (``~/.cursor/projects/.../mcps/user-mem0``). Tier 3
(MemU) calls will be wired once the MemU Python client is available on the
VPS — for now those tools return ``status: not_implemented`` with the canonical
payload so agents can still plan against them.

Tools registered:
    memory.add
    memory.search
    memu.broadcast
    memu.search
    learnings.read_last_3
"""

from __future__ import annotations

from typing import Any, Dict

from ..protocol import McpTool
from ..registry import ToolRegistry
from .context import ToolContext


def _ns(ctx: ToolContext, scope: str, company: str | None, agent: str | None) -> Dict[str, Any]:
    """Translate our (scope, company, agent) triple into mem0 identifiers."""
    scope = scope.lower()
    if scope == "agent":
        if not (company and agent):
            raise ValueError("scope=agent requires company+agent")
        return {
            "collection": f"tickles_{company}",
            "user_id": company,
            "agent_id": f"{company}_{agent}",
        }
    if scope == "company":
        if not company:
            raise ValueError("scope=company requires company")
        return {
            "collection": f"tickles_{company}",
            "user_id": company,
            "agent_id": "shared",
        }
    if scope == "building":
        return {
            "collection": "tickles_building",
            "user_id": "building",
            "agent_id": "shared",
        }
    raise ValueError(f"unknown scope: {scope}")


def _not_implemented(feature: str, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    out = {
        "status": "not_implemented",
        "feature": feature,
        "message": (
            "Tier-1/2 writes/reads go through the user-mem0 MCP server which "
            "the host (Cursor/OpenClaw) is already wired to. Tickles MCP "
            "returns the canonical (collection, user_id, agent_id) payload "
            "so the LLM client can forward the call to mem0."
        ),
    }
    if extra:
        out.update(extra)
    return out


def _build_tools(ctx: ToolContext) -> list[tuple[McpTool, Any]]:
    t_memory_add = McpTool(
        name="memory.add",
        description=(
            "Write to mem0 Tier-1 (agent-private) or Tier-2 (company-shared). "
            "Returns the canonical mem0 payload; the calling host must forward "
            "it to its mem0 MCP client."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["agent", "company", "building"],
                },
                "companyId": {"type": "string"},
                "agentId": {"type": "string"},
                "content": {"type": "string", "minLength": 1},
                "metadata": {"type": "object"},
            },
            "required": ["scope", "content"],
        },
        read_only=False,
        tags={"phase": "2", "group": "memory"},
    )

    async def _memory_add(p: Dict[str, Any]) -> Dict[str, Any]:
        ns = _ns(ctx, p["scope"], p.get("companyId"), p.get("agentId"))
        return {
            "forward_to": "user-mem0::add-memory",
            "arguments": {
                "content": p["content"],
                "metadata": p.get("metadata") or {},
                "namespace": ns["collection"],
                "user_id": ns["user_id"],
                "agent_id": ns["agent_id"],
            },
        }

    t_memory_search = McpTool(
        name="memory.search",
        description=(
            "Search mem0 at the requested scope. Returns the canonical mem0 "
            "payload for the host to forward."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["agent", "company", "building"],
                },
                "companyId": {"type": "string"},
                "agentId": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["scope", "query"],
        },
        read_only=True,
        tags={"phase": "2", "group": "memory"},
    )

    async def _memory_search(p: Dict[str, Any]) -> Dict[str, Any]:
        ns = _ns(ctx, p["scope"], p.get("companyId"), p.get("agentId"))
        return {
            "forward_to": "user-mem0::search-memory",
            "arguments": {
                "query": p["query"],
                "limit": p.get("limit", 10),
                "namespace": ns["collection"],
                "user_id": ns["user_id"],
                "agent_id": ns["agent_id"],
            },
        }

    t_memu_broadcast = McpTool(
        name="memu.broadcast",
        description=(
            "Tier-3 cross-company broadcast into MemU (Postgres+pgvector). "
            "Used by the Strategy Council Moderator after board meetings."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "originCompanyId": {"type": "string"},
                "originAgentId": {"type": "string"},
                "category": {
                    "type": "string",
                    "enum": ["lesson", "warning", "playbook", "postmortem"],
                },
                "content": {"type": "string"},
                "metadata": {"type": "object"},
            },
            "required": ["originCompanyId", "category", "content"],
        },
        read_only=False,
        tags={"phase": "2", "group": "memory", "status": "stub"},
    )

    async def _memu_broadcast(p: Dict[str, Any]) -> Dict[str, Any]:
        return _not_implemented("memu.broadcast", {"echo": p})

    t_memu_search = McpTool(
        name="memu.search",
        description="Search the cross-company MemU (Tier-3) for relevant insights.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {
                    "type": "string",
                    "enum": ["lesson", "warning", "playbook", "postmortem"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["query"],
        },
        read_only=True,
        tags={"phase": "2", "group": "memory", "status": "stub"},
    )

    async def _memu_search(p: Dict[str, Any]) -> Dict[str, Any]:
        return _not_implemented("memu.search", {"echo": p})

    t_learnings_last3 = McpTool(
        name="learnings.read_last_3",
        description=(
            "Read the last 3 learnings an agent wrote (Tier-1). Every routine "
            "must call this before a decision (Twilly Template 03)."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {"type": "string"},
                "agentId": {"type": "string"},
                "topic": {"type": "string"},
            },
            "required": ["companyId", "agentId"],
        },
        read_only=True,
        tags={"phase": "2", "group": "memory"},
    )

    async def _learnings_last3(p: Dict[str, Any]) -> Dict[str, Any]:
        ns = _ns(ctx, "agent", p["companyId"], p["agentId"])
        topic = p.get("topic")
        query = topic if topic else "recent learnings"
        return {
            "forward_to": "user-mem0::search-memory",
            "arguments": {
                "query": query,
                "limit": 3,
                "namespace": ns["collection"],
                "user_id": ns["user_id"],
                "agent_id": ns["agent_id"],
                "memory_types": ["learning", "postmortem", "autopsy"],
            },
        }

    return [
        (t_memory_add, _memory_add),
        (t_memory_search, _memory_search),
        (t_memu_broadcast, _memu_broadcast),
        (t_memu_search, _memu_search),
        (t_learnings_last3, _learnings_last3),
    ]


def register(registry: ToolRegistry, ctx: ToolContext) -> None:
    for tool, handler in _build_tools(ctx):
        registry.register(tool, handler)
