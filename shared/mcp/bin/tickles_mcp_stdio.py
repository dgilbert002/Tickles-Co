"""Tickles MCP stdio entrypoint for OpenClaw / Claude Desktop."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

from ..memory_pool import InMemoryMcpPool
from ..registry import (
    ToolRegistry,
    build_ping_tool,
    register_builtin_providers,
)
from ..server import McpServer
from ..store import InvocationStore
from ..tools import ToolContext, provisioning, data, memory, trading, learning, backtest, meta, contest
from ..transports.stdio import run_stdio


LOG = logging.getLogger("tickles.mcp.stdio-entry")


# M5 (2026-04-20): stdio entry now calls `register_builtin_providers`
# so OpenClaw-spawned stdio clients see the same 41-tool catalogue as
# the HTTP daemon on :7777.
#
# M9 (2026-04-21): replaced InMemoryMcpPool with real DatabasePool
# for invocation recording. Falls back to InMemoryMcpPool if Postgres
# is unavailable at startup.


def build_registry(ctx: ToolContext) -> ToolRegistry:
    """Build the tool registry with all providers registered."""
    reg = ToolRegistry()
    ping_tool, ping_handler = build_ping_tool()
    reg.register(ping_tool, ping_handler)
    provisioning.register(reg, ctx)
    data.register(reg, ctx)
    memory.register(reg, ctx)
    trading.register(reg, ctx)
    backtest.register(reg, ctx)
    learning.register(reg, ctx)
    meta.register(reg, ctx)
    contest.register(reg, ctx)
    register_builtin_providers(reg, LOG)
    return reg


async def _init_pool() -> tuple[Any, str]:
    """Try to initialise a real Postgres pool; fall back to in-memory.

    Returns:
        Tuple of (pool, pool_type) where pool_type is "postgres" or
        "in_memory".
    """
    try:
        from shared.utils.db import DatabasePool

        pool = await DatabasePool.get_instance()
        return pool, "postgres"
    except Exception as exc:
        LOG.warning(
            "[stdio] Postgres unavailable, falling back to in-memory: %s",
            exc,
        )
        return InMemoryMcpPool(), "in_memory"


async def _run() -> None:
    """Create the server and start the stdio listener."""
    logging.basicConfig(
        level=os.environ.get("TICKLES_LOGLEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    ctx = ToolContext()

    pool, pool_type = await _init_pool()
    store = InvocationStore(pool)
    registry = build_registry(ctx)
    server = McpServer(
        registry=registry,
        invocation_store=store,
        default_caller="openclaw",
        default_transport="stdio",
    )
    LOG.info(
        "[main] starting tickles-mcp-stdio (tools=%d, pool=%s)",
        len(registry.list_tools()),
        pool_type,
    )
    await run_stdio(server, caller="openclaw")


def main() -> int:
    """CLI entrypoint for the MCP stdio transport."""
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
