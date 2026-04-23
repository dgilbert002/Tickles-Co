"""HTTP transport for the MCP server.

Exposes a single POST endpoint (/mcp) that accepts a JSON-RPC payload
and returns the JSON-RPC response. Auth uses a shared bearer token
(pass ``auth_token`` at app build time). Rate limiting is applied per
client IP with a sliding-window algorithm.

Endpoints:
    POST /mcp       — JSON-RPC 2.0 tool calls
    GET  /healthz   — health check (no auth required)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from aiohttp import web

from ..server import McpServer


log = logging.getLogger("tickles.mcp.http")

# ---- Rate limiter -------------------------------------------------------

_DEFAULT_RATE_LIMIT: int = int(os.environ.get("TICKLES_MCP_RATE_LIMIT", "120"))
_DEFAULT_RATE_BURST: int = int(os.environ.get("TICKLES_MCP_RATE_BURST", "20"))
_RATE_WINDOW_SECONDS: int = 60


class _SlidingWindowLimiter:
    """Per-IP sliding-window rate limiter.

    Args:
        max_requests: Maximum requests allowed within the window.
        window_seconds: Window duration in seconds.
        burst: Maximum requests allowed in a single second.
    """

    def __init__(
        self,
        max_requests: int = _DEFAULT_RATE_LIMIT,
        window_seconds: int = _RATE_WINDOW_SECONDS,
        burst: int = _DEFAULT_RATE_BURST,
    ) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._burst = burst
        self._timestamps: Dict[str, List[float]] = defaultdict(list)

    def is_allowed(self, client_id: str) -> bool:
        """Check if a request from client_id is allowed.

        Args:
            client_id: Client identifier (usually IP address).

        Returns:
            True if the request is allowed, False if rate-limited.
        """
        now = time.monotonic()
        cutoff = now - self._window
        timestamps = self._timestamps[client_id]

        # Prune old entries
        while timestamps and timestamps[0] < cutoff:
            timestamps.pop(0)

        # Check window limit
        if len(timestamps) >= self._max:
            return False

        # Check burst limit (requests in last second)
        burst_cutoff = now - 1.0
        recent = sum(1 for t in timestamps if t >= burst_cutoff)
        if recent >= self._burst:
            return False

        timestamps.append(now)
        return True

    def reset(self, client_id: str) -> None:
        """Reset rate limit state for a client.

        Args:
            client_id: Client identifier to reset.
        """
        self._timestamps.pop(client_id, None)


# ---- App builder --------------------------------------------------------


def build_http_app(
    server: McpServer,
    *,
    auth_token: Optional[str] = None,
    rate_limit: int = _DEFAULT_RATE_LIMIT,
    rate_burst: int = _DEFAULT_RATE_BURST,
    db_pool: Any = None,
) -> web.Application:
    """Build the aiohttp application with auth, rate limiting, and health.

    Args:
        server: The McpServer instance to dispatch requests to.
        auth_token: Bearer token for auth (None = no auth required).
        rate_limit: Max requests per minute per IP.
        rate_burst: Max requests per second per IP.
        db_pool: Optional DatabasePool for health checks. When provided,
            the /healthz endpoint will verify DB connectivity.

    Returns:
        Configured aiohttp Application.
    """
    limiter = _SlidingWindowLimiter(
        max_requests=rate_limit,
        burst=rate_burst,
    )
    started_at = time.monotonic()

    @web.middleware
    async def auth_middleware(
        request: web.Request, handler: Any
    ) -> web.StreamResponse:
        """Enforce bearer token auth on non-health endpoints."""
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

    @web.middleware
    async def rate_limit_middleware(
        request: web.Request, handler: Any
    ) -> web.StreamResponse:
        """Enforce per-IP rate limiting on MCP calls."""
        if request.path in ("/healthz",):
            return await handler(request)
        client_ip = request.remote or "unknown"
        if not limiter.is_allowed(client_ip):
            log.warning("rate limited: ip=%s path=%s", client_ip, request.path)
            return web.json_response(
                {"error": "rate limited", "retry_after": _RATE_WINDOW_SECONDS},
                status=429,
            )
        return await handler(request)

    app = web.Application(middlewares=[auth_middleware, rate_limit_middleware])
    app["mcp_server"] = server
    app["mcp_auth_token"] = auth_token
    app["mcp_started_at"] = started_at
    app["mcp_db_pool"] = db_pool

    async def healthz(request: web.Request) -> web.Response:
        """Enhanced health check endpoint.

        Returns server status, tool count, DB connectivity, and uptime.
        No auth required.
        """
        srv: McpServer = request.app["mcp_server"]
        uptime = int(time.monotonic() - request.app["mcp_started_at"])
        tool_count = len(srv._registry.list_tools())

        # Check DB connectivity via the dedicated pool
        db_status = "not_configured"
        pool = request.app.get("mcp_db_pool")
        if pool is not None:
            try:
                async with pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
                db_status = "connected"
            except Exception as exc:
                log.warning("healthz: db check failed: %s", exc)
                db_status = f"error: {exc}"
        elif srv._invocations is not None:
            db_status = "in_memory"

        return web.json_response({
            "ok": True,
            "tools": tool_count,
            "db": db_status,
            "uptime_seconds": uptime,
            "auth_enabled": bool(auth_token),
            "rate_limit": rate_limit,
            "rate_burst": rate_burst,
        })

    async def mcp_handler(request: web.Request) -> web.Response:
        """Handle JSON-RPC 2.0 requests."""
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
    rate_limit: int = _DEFAULT_RATE_LIMIT,
    rate_burst: int = _DEFAULT_RATE_BURST,
    db_pool: Any = None,
) -> None:
    """Start the HTTP server and block until interrupted.

    Args:
        server: The McpServer instance.
        host: Bind address.
        port: Bind port.
        auth_token: Bearer token (None = no auth).
        rate_limit: Max requests per minute per IP.
        rate_burst: Max requests per second per IP.
        db_pool: Optional DatabasePool for health checks.
    """
    app = build_http_app(
        server,
        auth_token=auth_token,
        rate_limit=rate_limit,
        rate_burst=rate_burst,
        db_pool=db_pool,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info(
        "tickles-mcp http listening on %s:%d (auth=%s, rate_limit=%d/min, db=%s)",
        host,
        port,
        "bearer" if auth_token else "none",
        rate_limit,
        "postgres" if db_pool else "none",
    )
    try:
        while True:
            await asyncio.sleep(3600)
    finally:  # pragma: no cover - shutdown path
        await runner.cleanup()
