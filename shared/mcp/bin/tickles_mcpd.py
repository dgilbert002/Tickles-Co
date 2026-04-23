"""Tickles MCP daemon — production HTTP entrypoint on :7777.

Reads config from env vars:

    TICKLES_MCP_HOST              bind host (default 127.0.0.1)
    TICKLES_MCP_PORT              bind port (default 7777)
    TICKLES_MCP_TOKEN             bearer token (REQUIRED for non-loopback)
    TICKLES_MCP_RATE_LIMIT        max requests per minute per IP (default 120)
    TICKLES_MCP_RATE_BURST        max requests per second per IP (default 20)
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
from typing import Any, Iterable, Optional

from ..memory_pool import InMemoryMcpPool
from ..registry import (
    ToolRegistry,
    build_ping_tool,
    register_builtin_providers,
)
from ..server import McpServer
from ..store import InvocationStore
from ..transports.http import run_http
from ..tools import ToolContext
from ..tools import provisioning, data, memory, trading, learning, backtest, meta, contest


LOG = logging.getLogger("tickles.mcp.daemon")


# ---------------------------------------------------------------------
# M0.5 (2026-04-20): six built-in tool providers (services.list real,
# strategy.intents.recent / backtest.submit / backtest.status /
# dashboard.snapshot / regime.current as safe not_implemented stubs).
#
# Refactored 2026-04-20 (M5): the wiring previously lived here as a
# private helper (`_register_builtin_tool_providers`) but that meant
# the stdio entrypoint (shared/mcp/bin/tickles_mcp_stdio.py) shipped
# only 35 tools while this HTTP daemon shipped 41. The helper was
# promoted to `shared.mcp.registry.register_builtin_providers` so both
# entry points register the same catalogue.
#
# M9 (2026-04-21): replaced InMemoryMcpPool with real DatabasePool
# for invocation recording. Falls back to InMemoryMcpPool if Postgres
# is unavailable at startup. Added --rate-limit / --rate-burst CLI
# args and db_pool passthrough for /healthz DB connectivity checks.
# ---------------------------------------------------------------------


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

    LOG.info("[build_registry] registered tools=%d", len(reg.list_tools()))
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
            "[tickles_mcpd] Postgres unavailable, falling back to "
            "in-memory pool: %s",
            exc,
        )
        return InMemoryMcpPool(), "in_memory"


async def _run(
    host: str,
    port: int,
    token: Optional[str],
    rate_limit: int,
    rate_burst: int,
) -> None:
    """Create the server and start the HTTP listener."""
    ctx = ToolContext()
    registry = build_registry(ctx)

    pool, pool_type = await _init_pool()
    server = McpServer(
        registry,
        invocation_store=InvocationStore(pool),
        default_caller="http",
        default_transport="http",
    )
    db_pool = pool if pool_type == "postgres" else None
    LOG.info(
        "[tickles_mcpd] starting host=%s port=%d auth=%s pool=%s",
        host,
        port,
        "bearer" if token else "none-localhost-only",
        pool_type,
    )
    await run_http(
        server,
        host=host,
        port=port,
        auth_token=token,
        rate_limit=rate_limit,
        rate_burst=rate_burst,
        db_pool=db_pool,
    )


def main(argv: Iterable[str] | None = None) -> int:
    """CLI entrypoint for the MCP daemon."""
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
        "--rate-limit",
        type=int,
        default=int(os.environ.get("TICKLES_MCP_RATE_LIMIT", "120")),
        help="Max requests per minute per IP (default: 120).",
    )
    parser.add_argument(
        "--rate-burst",
        type=int,
        default=int(os.environ.get("TICKLES_MCP_RATE_BURST", "20")),
        help="Max requests per second per IP (default: 20).",
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
        asyncio.run(
            _run(args.host, args.port, args.token, args.rate_limit, args.rate_burst)
        )
    except KeyboardInterrupt:
        LOG.info("[main] interrupted, shutting down")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
