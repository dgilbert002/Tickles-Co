"""Tickles MCP daemon — production HTTP entrypoint on :7777.

Reads config from env vars:

    TICKLES_MCP_HOST              bind host (default 127.0.0.1)
    TICKLES_MCP_PORT              bind port (default 7777)
    TICKLES_MCP_TOKEN             bearer token (REQUIRED for non-loopback)
    PAPERCLIP_URL                 Paperclip API (default http://127.0.0.1:3100)
    PAPERCLIP_API_TOKEN           optional bearer for Paperclip
    MEM0_MCP_URL                  mem0 MCP (optional)
    MEMU_DSN                      MemU Postgres DSN (optional)
    TICKLES_LOGLEVEL              INFO|DEBUG (default INFO)

It registers the built-in ping tool plus the five Phase-2 tool groups
(provisioning, data, memory, trading, learning).

Example: ::

    sudo TICKLES_MCP_TOKEN=$(openssl rand -hex 24) \\
         python3 -m shared.mcp.bin.tickles_mcpd

or as systemd (see ``shared/mcp/systemd/tickles-mcpd.service``).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Iterable

from ..memory_pool import InMemoryMcpPool
from ..registry import ToolRegistry, build_ping_tool
from ..server import McpServer
from ..store import InvocationStore
from ..transports.http import run_http
from ..tools import ToolContext
from ..tools import provisioning, data, memory, trading, learning


LOG = logging.getLogger("tickles.mcp.daemon")


def build_registry(ctx: ToolContext) -> ToolRegistry:
    reg = ToolRegistry()

    ping_tool, ping_handler = build_ping_tool()
    reg.register(ping_tool, ping_handler)

    provisioning.register(reg, ctx)
    data.register(reg, ctx)
    memory.register(reg, ctx)
    trading.register(reg, ctx)
    learning.register(reg, ctx)

    LOG.info("[build_registry] registered tools=%d", len(reg.list_tools()))
    return reg


async def _run(host: str, port: int, token: str | None) -> None:
    ctx = ToolContext()
    registry = build_registry(ctx)
    pool = InMemoryMcpPool()
    server = McpServer(
        registry,
        invocation_store=InvocationStore(pool),
        default_caller="http",
        default_transport="http",
    )
    LOG.info(
        "[tickles_mcpd] starting host=%s port=%d auth=%s",
        host, port, "bearer" if token else "none-localhost-only",
    )
    await run_http(server, host=host, port=port, auth_token=token)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tickles-mcpd", description=__doc__)
    parser.add_argument(
        "--host",
        default=os.environ.get("TICKLES_MCP_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("TICKLES_MCP_PORT", "7777")),
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("TICKLES_MCP_TOKEN"),
        help="Bearer token (required when binding a non-loopback address).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("TICKLES_LOGLEVEL", "INFO"),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token:
        LOG.error(
            "[main] refusing to bind %s without a bearer token", args.host
        )
        return 2

    try:
        asyncio.run(_run(args.host, args.port, args.token))
    except KeyboardInterrupt:
        LOG.info("[main] interrupted, shutting down")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
