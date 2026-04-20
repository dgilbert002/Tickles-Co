"""Tickles MCP stdio entrypoint for OpenClaw / Claude Desktop."""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from ..memory_pool import InMemoryMcpPool
from ..registry import ToolRegistry, build_ping_tool
from ..server import McpServer
from ..store import InvocationStore
from ..tools import ToolContext, provisioning, data, memory, trading, learning
from ..transports.stdio import run_stdio


LOG = logging.getLogger("tickles.mcp.stdio-entry")


def build_registry(ctx: ToolContext) -> ToolRegistry:
    reg = ToolRegistry()
    ping_tool, ping_handler = build_ping_tool()
    reg.register(ping_tool, ping_handler)
    provisioning.register(reg, ctx)
    data.register(reg, ctx)
    memory.register(reg, ctx)
    trading.register(reg, ctx)
    learning.register(reg, ctx)
    return reg


async def _run() -> None:
    logging.basicConfig(
        level=os.environ.get("TICKLES_LOGLEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    ctx = ToolContext()
    pool = InMemoryMcpPool()
    store = InvocationStore(pool)
    registry = build_registry(ctx)
    server = McpServer(registry=registry, invocation_store=store, default_caller="openclaw", default_transport="stdio")
    LOG.info("[main] starting tickles-mcp-stdio (tools=%d)", len(registry.list_tools()))
    await run_stdio(server, caller="openclaw")


def main() -> int:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
