"""In-memory pool that mimics the asyncpg interface used by the
Phase-37 MCP stores.

Only the subset of SQL used by `ToolCatalogStore` and `InvocationStore`
is supported - this lets us test without Postgres.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


class _Row(dict):
    def __getitem__(self, key: Any) -> Any:
        return super().__getitem__(key)


class _Conn:
    def __init__(self, pool: "InMemoryMcpPool") -> None:
        self._pool = pool

    async def execute(self, sql: str, *args: Any) -> str:
        normalised = " ".join(sql.split()).lower()
        if normalised.startswith("insert into public.mcp_tools"):
            self._pool._upsert_tool(args)
            return "INSERT 0 1"
        if normalised.startswith(
            "update public.mcp_tools set enabled"
        ):
            name = args[0]
            enabled = bool(args[1])
            row = self._pool._tools.get(name)
            if row is not None:
                row["enabled"] = enabled
                row["updated_at"] = _now()
            return "UPDATE 1"
        raise NotImplementedError(f"execute: {sql!r}")

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        normalised = " ".join(sql.split()).lower()
        if normalised.startswith("insert into public.mcp_invocations"):
            inv_id = self._pool._insert_invocation(args)
            return _Row(id=inv_id)
        raise NotImplementedError(f"fetchrow: {sql!r}")

    async def fetch(self, sql: str, *args: Any) -> List[Any]:
        normalised = " ".join(sql.split()).lower()
        if "from public.mcp_tools where enabled = true" in normalised:
            rows = [
                self._pool._tools[name]
                for name in sorted(self._pool._tools)
                if self._pool._tools[name].get("enabled", True)
            ]
            return [_Row(**row) for row in rows]
        if (
            "from public.mcp_tools" in normalised
            and "where enabled" not in normalised
        ):
            rows = [
                self._pool._tools[name]
                for name in sorted(self._pool._tools)
            ]
            return [_Row(**row) for row in rows]
        if "from public.mcp_invocations" in normalised:
            limit = int(args[0]) if args else 50
            rows = sorted(
                self._pool._invocations,
                key=lambda r: r.get("started_at") or _now(),
                reverse=True,
            )
            rows = rows[:limit]
            return [_Row(**row) for row in rows]
        raise NotImplementedError(f"fetch: {sql!r}")


class InMemoryMcpPool:
    def __init__(self) -> None:
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._invocations: List[Dict[str, Any]] = []
        self._next_inv_id = 1
        self._lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def acquire(self):
        async with self._lock:
            yield _Conn(self)

    # --- internal helpers used by _Conn ---

    def _upsert_tool(self, args: Tuple[Any, ...]) -> None:
        (
            name,
            version,
            description,
            input_schema,
            output_schema,
            read_only,
            enabled,
            tags,
        ) = args
        row = {
            "name": name,
            "version": version,
            "description": description,
            "input_schema": _parse_json(input_schema) or {},
            "output_schema": _parse_json(output_schema) or {},
            "read_only": bool(read_only),
            "enabled": bool(enabled),
            "tags": _parse_json(tags) or {},
            "created_at": self._tools.get(name, {}).get(
                "created_at"
            )
            or _now(),
            "updated_at": _now(),
        }
        self._tools[name] = row

    def _insert_invocation(self, args: Tuple[Any, ...]) -> int:
        (
            tool_name,
            tool_version,
            caller,
            transport,
            params,
            status,
            result,
            error,
            latency_ms,
            started_at,
            completed_at,
        ) = args
        inv_id = self._next_inv_id
        self._next_inv_id += 1
        self._invocations.append(
            {
                "id": inv_id,
                "tool_name": tool_name,
                "tool_version": tool_version,
                "caller": caller,
                "transport": transport,
                "params": _parse_json(params) or {},
                "status": status,
                "result": _parse_json(result),
                "error": error,
                "latency_ms": latency_ms,
                "started_at": started_at,
                "completed_at": completed_at,
            }
        )
        return inv_id
