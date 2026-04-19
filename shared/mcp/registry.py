"""In-process MCP tool registry.

This is the runtime counterpart of `public.mcp_tools`. Tool handlers
are Python callables (sync or async) that accept a dict of params and
return a JSON-serialisable value.

The registry deliberately stays decoupled from I/O: it doesn't know
about the transport or the audit store. `server.py` is responsible for
wiring those pieces together.
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .protocol import McpTool, ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, McpTool] = {}
        self._handlers: Dict[str, ToolHandler] = {}

    def register(self, tool: McpTool, handler: ToolHandler) -> None:
        self._tools[tool.name] = tool
        self._handlers[tool.name] = handler

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._handlers.pop(name, None)

    def get(self, name: str) -> Optional[McpTool]:
        return self._tools.get(name)

    def list_tools(
        self, *, include_disabled: bool = False
    ) -> List[McpTool]:
        out = sorted(self._tools.values(), key=lambda t: t.name)
        if include_disabled:
            return out
        return [t for t in out if t.enabled]

    def set_enabled(self, name: str, enabled: bool) -> None:
        tool = self._tools.get(name)
        if tool is not None:
            tool.enabled = enabled

    async def call(
        self, name: str, params: Dict[str, Any]
    ) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"tool not registered: {name}")
        if not tool.enabled:
            raise PermissionError(f"tool disabled: {name}")
        handler = self._handlers[name]
        result = handler(params or {})
        if inspect.isawaitable(result):
            return await result  # type: ignore[no-any-return]
        return result


# ---------------------------------------------------------------------
# Built-in tools.
#
# Each `build_*` helper returns (McpTool, handler) pairs that are
# wired into the registry by the server. We keep these small and
# deterministic so they're easy to test; real IO (services registry,
# backtest store, dashboard snapshot) is injected via providers.
# ---------------------------------------------------------------------


def build_ping_tool() -> tuple[McpTool, ToolHandler]:
    tool = McpTool(
        name="ping",
        description=(
            "Returns 'pong' plus a server timestamp. Useful as a "
            "heartbeat from LLM clients to verify connectivity."
        ),
        version="1",
        input_schema={"type": "object", "properties": {}},
        output_schema={
            "type": "object",
            "properties": {
                "pong": {"type": "boolean"},
                "ts": {"type": "string"},
            },
        },
        read_only=True,
        tags={"phase": "37", "kind": "diagnostic"},
    )

    def _handler(_: Dict[str, Any]) -> Dict[str, Any]:
        from .protocol import utcnow

        return {"pong": True, "ts": utcnow().isoformat()}

    return tool, _handler


def build_services_list_tool(
    services_provider: Callable[[], Awaitable[List[Dict[str, Any]]]]
) -> tuple[McpTool, ToolHandler]:
    tool = McpTool(
        name="services.list",
        description=(
            "Lists all Tickles long-running services as registered in "
            "the in-process ServiceRegistry. Read-only."
        ),
        version="1",
        input_schema={"type": "object", "properties": {}},
        output_schema={
            "type": "object",
            "properties": {
                "services": {"type": "array"},
            },
        },
        read_only=True,
        tags={"phase": "37", "kind": "services"},
    )

    async def _handler(_: Dict[str, Any]) -> Dict[str, Any]:
        services = await services_provider()
        return {"count": len(services), "services": services}

    return tool, _handler


def build_strategy_intents_tool(
    intents_provider: Callable[
        [int], Awaitable[List[Dict[str, Any]]]
    ]
) -> tuple[McpTool, ToolHandler]:
    tool = McpTool(
        name="strategy.intents.recent",
        description=(
            "Returns the most recent strategy intents emitted by the "
            "Phase-34 composer. Optional `limit` parameter (1..100)."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 10,
                }
            },
        },
        read_only=True,
        tags={"phase": "37", "kind": "strategy"},
    )

    async def _handler(params: Dict[str, Any]) -> Dict[str, Any]:
        limit = int(params.get("limit") or 10)
        limit = max(1, min(100, limit))
        intents = await intents_provider(limit)
        return {"count": len(intents), "intents": intents}

    return tool, _handler


def build_backtest_submit_tool(
    submit_fn: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]
) -> tuple[McpTool, ToolHandler]:
    tool = McpTool(
        name="backtest.submit",
        description=(
            "Submits a backtest spec to the Phase-35 submission "
            "layer. Idempotent by spec hash."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "spec": {
                    "type": "object",
                    "description": "BacktestSpec dict",
                }
            },
            "required": ["spec"],
        },
        read_only=False,
        tags={"phase": "37", "kind": "backtest"},
    )

    async def _handler(params: Dict[str, Any]) -> Dict[str, Any]:
        spec = params.get("spec")
        if not isinstance(spec, dict):
            raise ValueError("spec must be an object")
        return await submit_fn(spec)

    return tool, _handler


def build_backtest_status_tool(
    status_fn: Callable[[str], Awaitable[Optional[Dict[str, Any]]]]
) -> tuple[McpTool, ToolHandler]:
    tool = McpTool(
        name="backtest.status",
        description=(
            "Looks up a backtest submission by spec_hash or "
            "submission_id."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "spec_hash or submission id",
                }
            },
            "required": ["key"],
        },
        read_only=True,
        tags={"phase": "37", "kind": "backtest"},
    )

    async def _handler(params: Dict[str, Any]) -> Dict[str, Any]:
        key = str(params.get("key") or "").strip()
        if not key:
            raise ValueError("key is required")
        row = await status_fn(key)
        return {"found": row is not None, "submission": row}

    return tool, _handler


def build_dashboard_snapshot_tool(
    snapshot_fn: Callable[[], Awaitable[Dict[str, Any]]]
) -> tuple[McpTool, ToolHandler]:
    tool = McpTool(
        name="dashboard.snapshot",
        description=(
            "Returns the Phase-36 dashboard snapshot (services, "
            "submissions, intents, regime, guardrails) as JSON."
        ),
        version="1",
        input_schema={"type": "object", "properties": {}},
        read_only=True,
        tags={"phase": "37", "kind": "dashboard"},
    )

    async def _handler(_: Dict[str, Any]) -> Dict[str, Any]:
        return await snapshot_fn()

    return tool, _handler


def build_regime_current_tool(
    regime_fn: Callable[[], Awaitable[Dict[str, Any]]]
) -> tuple[McpTool, ToolHandler]:
    tool = McpTool(
        name="regime.current",
        description=(
            "Returns the current regime classification as emitted "
            "by Phase-27 RegimeService."
        ),
        version="1",
        input_schema={"type": "object", "properties": {}},
        read_only=True,
        tags={"phase": "37", "kind": "regime"},
    )

    async def _handler(_: Dict[str, Any]) -> Dict[str, Any]:
        return await regime_fn()

    return tool, _handler


# Small helper used by CLI / server when providers are missing.


async def _empty_list() -> List[Dict[str, Any]]:
    return []


async def _empty_snapshot() -> Dict[str, Any]:
    return {"available": False}
