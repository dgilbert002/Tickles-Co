"""HTTP transport for the MCP server.

Exposes a single POST endpoint (/mcp) that accepts a JSON-RPC payload
and returns the JSON-RPC response. Auth uses a shared bearer token
(pass `auth_token` at app build time). For production deployments
you'd put this behind Tailscale + the Phase-36 session tokens, but
those concerns live in the deploy layer, not here.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from aiohttp import web

from ..server import McpServer


log = logging.getLogger("tickles.mcp.http")


def build_http_app(
    server: McpServer,
    *,
    auth_token: Optional[str] = None,
) -> web.Application:
    @web.middleware
    async def auth_middleware(
        request: web.Request, handler: Any
    ) -> web.StreamResponse:
        if request.path in ("/healthz",):
            return await handler(request)
        token = request.app["mcp_auth_token"]
        if token:
            got = request.headers.get("Authorization", "")
            if not got.startswith("Bearer "):
                return web.json_response(
                    {"error": "missing bearer"}, status=401
                )
            if got[len("Bearer ") :] != token:
                return web.json_response(
                    {"error": "bad bearer"}, status=401
                )
        return await handler(request)

    app = web.Application(middlewares=[auth_middleware])
    app["mcp_server"] = server
    app["mcp_auth_token"] = auth_token

    async def healthz(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def mcp_handler(request: web.Request) -> web.Response:
        try:
            payload: Dict[str, Any] = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "parse error"},
                },
                status=200,
            )
        caller = request.headers.get("X-MCP-Caller", "http")
        resp = await server.handle(
            payload, caller=caller, transport="http"
        )
        if resp is None:
            return web.Response(status=204)
        return web.json_response(resp)

    app.router.add_get("/healthz", healthz)
    app.router.add_post("/mcp", mcp_handler)
    return app


async def run_http(
    server: McpServer,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    auth_token: Optional[str] = None,
) -> None:
    app = build_http_app(server, auth_token=auth_token)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info(
        "tickles-mcp http listening on %s:%d (auth=%s)",
        host,
        port,
        "bearer" if auth_token else "none",
    )
    try:
        import asyncio

        while True:
            await asyncio.sleep(3600)
    finally:  # pragma: no cover - shutdown path
        await runner.cleanup()
