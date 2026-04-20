#!/bin/bash
# Phase A2 (retry): Build a tiny stdio launcher for Tickles MCP, then register with OpenClaw.
set -u

LOG=/root/rubicon-deploy.log
log() { echo "$(date -u +%FT%TZ) [phase-a2] $*" | tee -a "$LOG"; }

log "A2 retry: writing stdio launcher"

cat > /opt/tickles/shared/mcp/bin/tickles_mcp_stdio.py <<'PY'
"""Tickles MCP stdio entrypoint.

Mirrors tickles_mcpd.py but uses stdio transport so tools like
OpenClaw / Claude Desktop can spawn us as a subprocess.

Usage: python3 -m shared.mcp.bin.tickles_mcp_stdio
"""
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
    LOG.info("[build_registry] registered tools=%d", len(reg.list_tools()))
    return reg


async def _run() -> None:
    logging.basicConfig(
        level=os.environ.get("TICKLES_LOGLEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,  # IMPORTANT: stdout is reserved for JSON-RPC
    )
    ctx = ToolContext(
        paperclip_url=os.environ.get("PAPERCLIP_URL", "http://127.0.0.1:3100"),
        paperclip_token=os.environ.get("PAPERCLIP_API_TOKEN"),
        mem0_url=os.environ.get("MEM0_MCP_URL"),
        memu_dsn=os.environ.get("MEMU_DSN"),
    )
    pool = InMemoryMcpPool()
    store = InvocationStore(pool)
    registry = build_registry(ctx)
    server = McpServer(registry=registry, store=store)
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
PY
log "A2 retry: stdio launcher written"

# Smoke test: send a single initialize request, confirm we get JSON response back
log "A2 retry: smoke test stdio launcher (send initialize, read one line)"
cd /opt/tickles
(
  echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}'
  sleep 1
) | timeout 15 env PYTHONPATH=/opt/tickles python3 -m shared.mcp.bin.tickles_mcp_stdio 2>/tmp/mcp_stderr.log | head -3
log "A2 retry: stderr log snippet:"
head -20 /tmp/mcp_stderr.log 2>/dev/null | tee -a "$LOG"

# Check the ToolContext import signature - if it fails, fix it
log "A2 retry: ToolContext signature check"
grep -A 10 'class ToolContext' /opt/tickles/shared/mcp/tools/__init__.py 2>/dev/null | head -20 | tee -a "$LOG"

# Register with OpenClaw - stdio style
log "A2 retry: openclaw mcp set tickles ..."
openclaw mcp set tickles '{"command":"python3","args":["-m","shared.mcp.bin.tickles_mcp_stdio"],"env":{"PYTHONPATH":"/opt/tickles","PAPERCLIP_URL":"http://127.0.0.1:3100"},"cwd":"/opt/tickles"}' 2>&1 | tail -10 | tee -a "$LOG"

log "A2 retry: openclaw mcp list after:"
openclaw mcp list 2>&1 | head -20 | tee -a "$LOG"

log "A2 retry: openclaw mcp show tickles:"
openclaw mcp show tickles 2>&1 | head -20 | tee -a "$LOG"

# Progress check: funding collector should have written a poll by now
log "A2 retry: funding-collector progress check"
psql -h 127.0.0.1 -p 5432 -U admin -d tickles_shared -At -c "SELECT COUNT(*), MIN(snapshot_at), MAX(snapshot_at) FROM derivatives_snapshots;" 2>&1 | tee -a "$LOG"
