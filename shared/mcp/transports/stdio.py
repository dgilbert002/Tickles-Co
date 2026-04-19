"""JSON-RPC over stdio transport for the MCP server.

Each line on stdin is one JSON-RPC request; each response is one line
on stdout. This matches how Claude Desktop and other MCP clients spawn
local tool servers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, Optional, TextIO

from ..server import McpServer


log = logging.getLogger("tickles.mcp.stdio")


async def run_stdio(
    server: McpServer,
    *,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    caller: str = "stdio",
) -> None:
    """Reads newline-delimited JSON from stdin and writes responses
    to stdout until EOF. Designed to be launched as a subprocess by an
    MCP-capable client.
    """
    in_stream = stdin if stdin is not None else sys.stdin
    out_stream = stdout if stdout is not None else sys.stdout

    loop = asyncio.get_running_loop()

    while True:
        line = await loop.run_in_executor(None, in_stream.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(
                out_stream,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32700,
                        "message": f"parse error: {exc}",
                    },
                },
            )
            continue

        response = await server.handle(
            payload, caller=caller, transport="stdio"
        )
        if response is not None:
            _write(out_stream, response)


def _write(stream: TextIO, obj: Any) -> None:
    stream.write(json.dumps(obj) + "\n")
    stream.flush()
