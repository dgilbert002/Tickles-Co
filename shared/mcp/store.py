"""Phase 37 MCP store wrappers.

Two stores live here:

    ToolCatalogStore     - upserts / lists rows in public.mcp_tools so
                           operators can observe (and optionally
                           toggle) what agents can reach.
    InvocationStore      - append-only writes to public.mcp_invocations
                           for audit / observability.

Both stores accept anything that quacks like `shared.backtest_submit`
pool (`acquire()` returns a connection with async `fetch`, `fetchrow`,
`execute`). This lets us reuse `_AsyncpgPool` from backtest_cli and the
lightweight `InMemoryMcpPool` below in tests.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .protocol import McpInvocation, McpTool


def _json(value: Any) -> str:
    return json.dumps(value, default=str)


def _load_json(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


class ToolCatalogStore:
    """Upserts / reads public.mcp_tools."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def upsert(self, tool: McpTool) -> None:
        sql = (
            "INSERT INTO public.mcp_tools "
            "(name, version, description, input_schema, output_schema, "
            " read_only, enabled, tags, updated_at) "
            "VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7, "
            "        $8::jsonb, NOW()) "
            "ON CONFLICT (name) DO UPDATE SET "
            "  version       = EXCLUDED.version, "
            "  description   = EXCLUDED.description, "
            "  input_schema  = EXCLUDED.input_schema, "
            "  output_schema = EXCLUDED.output_schema, "
            "  read_only     = EXCLUDED.read_only, "
            "  enabled       = EXCLUDED.enabled, "
            "  tags          = EXCLUDED.tags, "
            "  updated_at    = NOW()"
        )
        async with self._pool.acquire() as conn:
            await conn.execute(
                sql,
                tool.name,
                tool.version,
                tool.description,
                _json(tool.input_schema),
                _json(tool.output_schema),
                tool.read_only,
                tool.enabled,
                _json(tool.tags),
            )

    async def list_enabled(self) -> List[McpTool]:
        sql = (
            "SELECT name, version, description, input_schema, "
            "       output_schema, read_only, enabled, tags "
            "FROM public.mcp_tools WHERE enabled = TRUE "
            "ORDER BY name"
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql)
        return [self._hydrate(row) for row in rows]

    async def list_all(self) -> List[McpTool]:
        sql = (
            "SELECT name, version, description, input_schema, "
            "       output_schema, read_only, enabled, tags "
            "FROM public.mcp_tools ORDER BY name"
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql)
        return [self._hydrate(row) for row in rows]

    async def set_enabled(self, name: str, enabled: bool) -> None:
        sql = (
            "UPDATE public.mcp_tools SET enabled = $2, updated_at = NOW() "
            "WHERE name = $1"
        )
        async with self._pool.acquire() as conn:
            await conn.execute(sql, name, enabled)

    @staticmethod
    def _hydrate(row: Any) -> McpTool:
        data = dict(row)
        return McpTool(
            name=str(data["name"]),
            description=str(data.get("description") or ""),
            version=str(data.get("version") or "1"),
            input_schema=_load_json(data.get("input_schema")),
            output_schema=_load_json(data.get("output_schema")),
            read_only=bool(data.get("read_only", True)),
            enabled=bool(data.get("enabled", True)),
            tags=_load_json(data.get("tags")),
        )


class InvocationStore:
    """Append-only writes to public.mcp_invocations."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def record(
        self,
        *,
        tool_name: str,
        tool_version: Optional[str],
        caller: Optional[str],
        transport: Optional[str],
        params: Dict[str, Any],
        status: str,
        result: Optional[Dict[str, Any]],
        error: Optional[str],
        latency_ms: Optional[int],
        started_at: datetime,
        completed_at: Optional[datetime],
    ) -> int:
        sql = (
            "INSERT INTO public.mcp_invocations "
            "(tool_name, tool_version, caller, transport, params, "
            " status, result, error, latency_ms, started_at, "
            " completed_at) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb, $8, "
            "        $9, $10, $11) "
            "RETURNING id"
        )
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                sql,
                tool_name,
                tool_version,
                caller,
                transport,
                _json(params or {}),
                status,
                _json(result) if result is not None else None,
                error,
                latency_ms,
                started_at,
                completed_at,
            )
        return int(row["id"]) if row else 0

    async def list_recent(self, limit: int = 50) -> List[McpInvocation]:
        sql = (
            "SELECT id, tool_name, tool_version, caller, transport, "
            "       params, status, result, error, latency_ms, "
            "       started_at, completed_at "
            "FROM public.mcp_invocations "
            "ORDER BY started_at DESC LIMIT $1"
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, int(limit))
        return [self._hydrate(row) for row in rows]

    @staticmethod
    def _hydrate(row: Any) -> McpInvocation:
        data = dict(row)
        started = data.get("started_at") or datetime.now(tz=timezone.utc)
        return McpInvocation(
            id=int(data["id"]) if data.get("id") is not None else None,
            tool_name=str(data["tool_name"]),
            tool_version=(
                str(data["tool_version"])
                if data.get("tool_version") is not None
                else None
            ),
            caller=(
                str(data["caller"])
                if data.get("caller") is not None
                else None
            ),
            transport=(
                str(data["transport"])
                if data.get("transport") is not None
                else None
            ),
            params=_load_json(data.get("params")),
            status=str(data.get("status") or "ok"),
            result=_load_json(data.get("result"))
            if data.get("result") is not None
            else None,
            error=(
                str(data["error"])
                if data.get("error") is not None
                else None
            ),
            latency_ms=(
                int(data["latency_ms"])
                if data.get("latency_ms") is not None
                else None
            ),
            started_at=started,
            completed_at=data.get("completed_at"),
        )


def invocation_to_dict(inv: McpInvocation) -> Dict[str, Any]:
    data = asdict(inv)
    # datetimes to ISO strings for JSON transport.
    for key in ("started_at", "completed_at"):
        val = data.get(key)
        if isinstance(val, datetime):
            data[key] = val.isoformat()
    return data
