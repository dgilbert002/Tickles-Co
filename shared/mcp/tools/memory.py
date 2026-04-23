"""MCP tools: Memory (Tier 1 mem0, Tier 2 shared, Tier 3 MemU).

The three-tier memory architecture (see ``.cursor/rules/tickles-co-architecture.mdc``):

* Tier 1 — agent-private mem0/Qdrant, scoped per-agent.
* Tier 2 — company-shared mem0/Qdrant, scoped per-company + SQL table.
* Tier 3 — cross-company MemU (Postgres + pgvector + pg_notify broadcast).

Wiring status (2026-04-20 post-M4):
  * Tier 1/2: ``memory.add`` / ``memory.search`` / ``learnings.read_last_3``
    now call ``shared.utils.mem0_config.ScopedMemory`` directly (local
    sentence-transformers embeddings + OpenRouter LLM with fallback chain
    + per-company Qdrant collection ``tickles_{company}``).  All mem0
    work runs inside ``asyncio.to_thread``.  If mem0 can't initialise
    (OpenRouter key missing, Qdrant unreachable, etc.) the tools fall
    back to the legacy ``forward_to: user-mem0::...`` envelope so an
    external MCP host (Cursor, stand-alone user-mem0) can still pick up
    the call — the envelope shape is preserved.
  * Tier 3 (M3): ``memu.broadcast`` / ``memu.search`` call the real
    :class:`shared.memu.client.MemU` synchronous client, also wrapped
    in ``asyncio.to_thread``.

Tools registered:
    memory.add             [Tier-1/2 live via ScopedMemory]
    memory.search          [Tier-1/2 live via ScopedMemory]
    memu.broadcast         [Tier-3 live]
    memu.search            [Tier-3 live]
    learnings.read_last_3  [Tier-1 live via ScopedMemory]
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, Optional

from ..protocol import McpTool
from ..registry import ToolRegistry
from .context import ToolContext


LOG = logging.getLogger("tickles.mcp.tools.memory")

# Lazy singleton for the MemU client. We do not construct it at import
# time because the daemon can start before Postgres is reachable; we try
# on the first call and cache the result (success or failure).
_MEMU_CLIENT: Any = None  # None = untried, False = init failed, else client
_MEMU_INIT_LOCK = threading.Lock()
_MEMU_LAST_ERROR: Optional[str] = None


def _get_memu_sync() -> Any:
    """Return a live MemU client, False on permanent init failure, or
    None if initialisation was not attempted yet (shouldn't happen after
    first call)."""
    global _MEMU_CLIENT, _MEMU_LAST_ERROR
    if _MEMU_CLIENT is not None:
        return _MEMU_CLIENT
    with _MEMU_INIT_LOCK:
        if _MEMU_CLIENT is not None:
            return _MEMU_CLIENT
        try:
            from shared.memu.client import get_memu  # lazy import
            client = get_memu()
            if getattr(client, "conn", None) is None:
                _MEMU_LAST_ERROR = (
                    "MemU client reports conn=None (MEMU_ENABLED=false "
                    "or connect failed silently)"
                )
                _MEMU_CLIENT = False
            else:
                _MEMU_CLIENT = client
                LOG.info("[memu] client initialised")
        except Exception as exc:  # noqa: BLE001 — catch-all intentional
            _MEMU_LAST_ERROR = f"{type(exc).__name__}: {exc}"
            LOG.warning("[memu] client init failed: %s", _MEMU_LAST_ERROR)
            _MEMU_CLIENT = False
    return _MEMU_CLIENT


# ---------------------------------------------------------------------
# M4 (2026-04-20): Tier-1/Tier-2 mem0 wiring via ScopedMemory.
#
# We cache one ScopedMemory per (company, agent_id) pair. ScopedMemory
# itself rebuilds a fresh `mem0.Memory.from_config(...)` on every call
# (so we don't need to worry about Qdrant connection lifecycle here).
# Cache lives for the lifetime of the daemon; clearing is not needed.
#
# If `mem0` / `shared.utils.mem0_config` cannot be imported, or init
# raises, the tools fall back to the legacy `forward_to` envelope so
# external MCP hosts (e.g. a stand-alone user-mem0) can still pick up
# the request. That keeps us dual-mode without forking the shape.
# ---------------------------------------------------------------------


_MEM0_AVAILABLE: Optional[bool] = None   # None = unknown, True/False = decided
_MEM0_IMPORT_ERROR: Optional[str] = None
_MEM0_CACHE: Dict[tuple, Any] = {}
_MEM0_LOCK = threading.Lock()


def _mem0_available() -> bool:
    """Detect once at first call whether ScopedMemory is importable.

    We deliberately do NOT construct a ScopedMemory here — ScopedMemory's
    constructor is cheap (no IO), but every add/search call rebuilds the
    full mem0.Memory pipeline; that's where network + heavy init happens.
    """
    global _MEM0_AVAILABLE, _MEM0_IMPORT_ERROR
    if _MEM0_AVAILABLE is not None:
        return _MEM0_AVAILABLE
    with _MEM0_LOCK:
        if _MEM0_AVAILABLE is not None:
            return _MEM0_AVAILABLE
        try:
            from shared.utils.mem0_config import ScopedMemory  # noqa: F401
            _MEM0_AVAILABLE = True
            LOG.info("[mem0] ScopedMemory importable")
        except Exception as exc:  # noqa: BLE001
            _MEM0_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
            LOG.warning("[mem0] ScopedMemory import failed: %s", _MEM0_IMPORT_ERROR)
            _MEM0_AVAILABLE = False
    return _MEM0_AVAILABLE


def _scoped_memory(company: str, agent_id: str) -> Any:
    """Return a cached ScopedMemory for (company, agent_id), or None on
    import failure. Thread-safe."""
    if not _mem0_available():
        return None
    key = (company, agent_id)
    cached = _MEM0_CACHE.get(key)
    if cached is not None:
        return cached
    with _MEM0_LOCK:
        cached = _MEM0_CACHE.get(key)
        if cached is not None:
            return cached
        from shared.utils.mem0_config import ScopedMemory
        sm = ScopedMemory(company=company, agent_id=agent_id)
        _MEM0_CACHE[key] = sm
        return sm


def _forward_envelope_add(ns: Dict[str, Any], content: str,
                          metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "forward_to": "user-mem0::add-memory",
        "arguments": {
            "content": content,
            "metadata": metadata,
            "namespace": ns["collection"],
            "user_id": ns["user_id"],
            "agent_id": ns["agent_id"],
        },
    }


def _forward_envelope_search(ns: Dict[str, Any], query: str, limit: int,
                             extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args: Dict[str, Any] = {
        "query": query,
        "limit": limit,
        "namespace": ns["collection"],
        "user_id": ns["user_id"],
        "agent_id": ns["agent_id"],
    }
    if extra:
        args.update(extra)
    return {
        "forward_to": "user-mem0::search-memory",
        "arguments": args,
    }


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
            "Persists via ScopedMemory — local sentence-transformers embeddings, "
            "per-company Qdrant collection tickles_<company>, OpenRouter LLM for "
            "fact extraction (with automatic fallback chain). Returns the mem0 "
            "write result. If mem0 is not available, returns a forward_to "
            "envelope so an external user-mem0 MCP host can pick up the call."
        ),
        version="2",
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
        content = p["content"]
        metadata = p.get("metadata") or {}

        # Companyscope in ScopedMemory = the collection suffix.
        # We derive it from ns["collection"] = "tickles_<company>".
        collection = ns["collection"]
        company = collection[len("tickles_"):] if collection.startswith("tickles_") else collection
        sm = _scoped_memory(company, ns["agent_id"])
        if sm is None:
            return _forward_envelope_add(ns, content, metadata)

        def _add() -> Any:
            return sm.add(
                content,
                user_id=ns["user_id"],
                agent_id=ns["agent_id"],
                metadata=metadata,
            )

        try:
            result = await asyncio.to_thread(_add)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("[memory.add] mem0 call failed, returning forward envelope")
            envelope = _forward_envelope_add(ns, content, metadata)
            envelope["mem0_error"] = f"{type(exc).__name__}: {exc}"
            return envelope

        return {
            "status": "ok",
            "scope": p["scope"],
            "collection": collection,
            "user_id": ns["user_id"],
            "agent_id": ns["agent_id"],
            "mem0_result": result,
        }

    t_memory_search = McpTool(
        name="memory.search",
        description=(
            "Semantic search over mem0 at the requested scope. Uses the "
            "per-company Qdrant collection tickles_<company> with local "
            "sentence-transformers embeddings (same vector space as writes). "
            "Returns {status, count, results} where each result has the "
            "memory text + metadata + relevance score. If mem0 is not "
            "available, returns a forward_to envelope."
        ),
        version="2",
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
        query = p["query"]
        limit = int(p.get("limit") or 10)
        limit = max(1, min(50, limit))

        collection = ns["collection"]
        company = collection[len("tickles_"):] if collection.startswith("tickles_") else collection
        sm = _scoped_memory(company, ns["agent_id"])
        if sm is None:
            return _forward_envelope_search(ns, query, limit)

        def _do_search() -> Any:
            return sm.search(
                query,
                user_id=ns["user_id"],
                agent_id=ns["agent_id"],
                limit=limit,
            )

        try:
            result = await asyncio.to_thread(_do_search)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("[memory.search] mem0 call failed, returning forward envelope")
            envelope = _forward_envelope_search(ns, query, limit)
            envelope["mem0_error"] = f"{type(exc).__name__}: {exc}"
            return envelope

        # Normalise the shape: mem0 may return dict-with-results or a bare list.
        if isinstance(result, dict) and "results" in result:
            results = result["results"]
        elif isinstance(result, list):
            results = result
        else:
            results = [result] if result is not None else []

        return {
            "status": "ok",
            "scope": p["scope"],
            "collection": collection,
            "count": len(results),
            "results": results,
        }

    t_memu_broadcast = McpTool(
        name="memu.broadcast",
        description=(
            "Tier-3 cross-company broadcast into MemU "
            "(Postgres + pgvector + pg_notify). Dedupes on (category, "
            "content_hash); returns the insight id. Used by the Strategy "
            "Council Moderator after board meetings to share lessons/warnings "
            "across companies."
        ),
        version="2",
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
        tags={"phase": "2", "group": "memory", "status": "live"},
    )

    async def _memu_broadcast(p: Dict[str, Any]) -> Dict[str, Any]:
        client = _get_memu_sync()
        if not client:
            return _not_implemented(
                "memu.broadcast",
                {
                    "reason": _MEMU_LAST_ERROR or "MemU client unavailable",
                    "echo": p,
                },
            )

        origin_company = p["originCompanyId"]
        origin_agent = p.get("originAgentId") or "shared"
        category = p["category"]
        content = p["content"]
        metadata = dict(p.get("metadata") or {})
        metadata.setdefault("origin_company", origin_company)
        metadata.setdefault("origin_agent", origin_agent)

        source_agent = f"{origin_company}_{origin_agent}"[:64]

        def _write() -> str:
            return client.write_insight(
                kind=category,
                content=content,
                source_agent=source_agent,
                metadata=metadata,
            )

        try:
            insight_id = await asyncio.to_thread(_write)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("[memu.broadcast] write failed")
            return _not_implemented(
                "memu.broadcast",
                {"reason": f"{type(exc).__name__}: {exc}", "echo": p},
            )

        return {
            "status": "ok",
            "insight_id": insight_id,
            "kind": category,
            "source_agent": source_agent,
        }

    t_memu_search = McpTool(
        name="memu.search",
        description=(
            "Search the cross-company MemU (Tier-3) for relevant insights. "
            "Uses pgvector semantic similarity when embeddings are available, "
            "falls back to recency order otherwise. Optionally filter by "
            "category (lesson|warning|playbook|postmortem)."
        ),
        version="2",
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
        tags={"phase": "2", "group": "memory", "status": "live"},
    )

    async def _memu_search(p: Dict[str, Any]) -> Dict[str, Any]:
        client = _get_memu_sync()
        if not client:
            return _not_implemented(
                "memu.search",
                {
                    "reason": _MEMU_LAST_ERROR or "MemU client unavailable",
                    "echo": p,
                },
            )

        query = p["query"]
        category = p.get("category")
        limit = int(p.get("limit") or 10)
        limit = max(1, min(50, limit))

        def _do_search() -> list:
            return client.search(query, kind=category, k=limit)

        try:
            rows = await asyncio.to_thread(_do_search)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("[memu.search] search failed")
            return _not_implemented(
                "memu.search",
                {"reason": f"{type(exc).__name__}: {exc}", "echo": p},
            )

        def _safe(row: Dict[str, Any]) -> Dict[str, Any]:
            import datetime as _dt
            out: Dict[str, Any] = {}
            for k, v in row.items():
                if isinstance(v, _dt.datetime):
                    out[k] = v.isoformat()
                elif hasattr(v, "hex") and not isinstance(v, (bytes, bytearray)):
                    out[k] = str(v)
                else:
                    out[k] = v
            return out

        return {
            "status": "ok",
            "count": len(rows),
            "results": [_safe(r) for r in rows],
        }

    t_learnings_last3 = McpTool(
        name="learnings.read_last_3",
        description=(
            "Read the last 3 learnings an agent wrote (Tier-1 agent-private "
            "mem0). Every routine must call this before a decision (Twilly "
            "Template 03). Optional `topic` focuses the search; omit it for a "
            "generic 'recent learnings' query."
        ),
        version="2",
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

        collection = ns["collection"]
        company = collection[len("tickles_"):] if collection.startswith("tickles_") else collection
        sm = _scoped_memory(company, ns["agent_id"])
        if sm is None:
            return _forward_envelope_search(
                ns, query, 3,
                extra={"memory_types": ["learning", "postmortem", "autopsy"]},
            )

        def _do_search() -> Any:
            return sm.search(
                query,
                user_id=ns["user_id"],
                agent_id=ns["agent_id"],
                limit=3,
            )

        try:
            result = await asyncio.to_thread(_do_search)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("[learnings.read_last_3] mem0 call failed, returning forward envelope")
            envelope = _forward_envelope_search(
                ns, query, 3,
                extra={"memory_types": ["learning", "postmortem", "autopsy"]},
            )
            envelope["mem0_error"] = f"{type(exc).__name__}: {exc}"
            return envelope

        if isinstance(result, dict) and "results" in result:
            results = result["results"]
        elif isinstance(result, list):
            results = result
        else:
            results = [result] if result is not None else []

        return {
            "status": "ok",
            "scope": "agent",
            "collection": collection,
            "topic": topic,
            "count": len(results),
            "results": results,
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
