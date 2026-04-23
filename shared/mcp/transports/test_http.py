"""Tests for shared/mcp/transports/http.py — auth, rate limiting, healthz.

Covers:
    - _SlidingWindowLimiter (unit tests)
    - Auth middleware (integration via TestClient)
    - Rate limiting middleware (integration via TestClient)
    - /healthz endpoint (integration via TestClient)
"""

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import TestClient, TestServer

from shared.mcp.registry import ToolRegistry, build_ping_tool
from shared.mcp.server import McpServer
from shared.mcp.transports.http import (
    _SlidingWindowLimiter,
    build_http_app,
)


# ---- Helpers ---------------------------------------------------------------


def _make_server() -> McpServer:
    """Create a minimal McpServer with only the ping tool."""
    reg = ToolRegistry()
    ping_tool, ping_handler = build_ping_tool()
    reg.register(ping_tool, ping_handler)
    return McpServer(reg)


class _MockConn:
    """Mock asyncpg connection for healthz tests."""

    async def fetchval(self, query: str, *args: Any) -> int:
        """Simulate a successful SELECT 1."""
        return 1


class _MockAcquire:
    """Mock async context manager for pool.acquire()."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    async def __aenter__(self) -> _MockConn:
        if self._fail:
            raise ConnectionError("connection refused")
        return _MockConn()

    async def __aexit__(self, *args: Any) -> None:
        pass


class _MockPool:
    """Mock DatabasePool for healthz tests."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    def acquire(self) -> _MockAcquire:
        """Return a mock acquire context manager."""
        return _MockAcquire(fail=self._fail)


# ---- SlidingWindowLimiter --------------------------------------------------


class TestSlidingWindowLimiter(unittest.TestCase):
    """Tests for the _SlidingWindowLimiter rate limiter."""

    def test_allows_under_limit(self) -> None:
        """Requests under the window limit are allowed."""
        limiter = _SlidingWindowLimiter(max_requests=5, burst=5)
        for _ in range(5):
            self.assertTrue(limiter.is_allowed("client1"))

    def test_blocks_over_window_limit(self) -> None:
        """Requests exceeding the window limit are blocked."""
        limiter = _SlidingWindowLimiter(max_requests=3, burst=10)
        for _ in range(3):
            self.assertTrue(limiter.is_allowed("client1"))
        self.assertFalse(limiter.is_allowed("client1"))

    def test_blocks_burst(self) -> None:
        """Requests exceeding the burst limit are blocked."""
        limiter = _SlidingWindowLimiter(max_requests=100, burst=2)
        self.assertTrue(limiter.is_allowed("client1"))
        self.assertTrue(limiter.is_allowed("client1"))
        self.assertFalse(limiter.is_allowed("client1"))

    def test_independent_clients(self) -> None:
        """Rate limits are tracked independently per client."""
        limiter = _SlidingWindowLimiter(max_requests=2, burst=10)
        self.assertTrue(limiter.is_allowed("client1"))
        self.assertTrue(limiter.is_allowed("client1"))
        self.assertFalse(limiter.is_allowed("client1"))
        self.assertTrue(limiter.is_allowed("client2"))

    def test_reset_clears_state(self) -> None:
        """Resetting a client allows new requests."""
        limiter = _SlidingWindowLimiter(max_requests=1, burst=10)
        self.assertTrue(limiter.is_allowed("client1"))
        self.assertFalse(limiter.is_allowed("client1"))
        limiter.reset("client1")
        self.assertTrue(limiter.is_allowed("client1"))


# ---- Auth middleware -------------------------------------------------------


class TestAuthMiddleware(unittest.IsolatedAsyncioTestCase):
    """Tests for bearer token auth middleware."""

    async def test_no_auth_configured_passes(self) -> None:
        """Requests pass when no auth token is configured."""
        server = _make_server()
        app = build_http_app(server, auth_token=None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            )
            self.assertNotEqual(resp.status, 401)

    async def test_valid_bearer_passes(self) -> None:
        """Requests with a valid bearer token pass auth."""
        server = _make_server()
        app = build_http_app(server, auth_token="secret123")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                headers={"Authorization": "Bearer secret123"},
            )
            self.assertNotEqual(resp.status, 401)

    async def test_missing_bearer_returns_401(self) -> None:
        """Requests without Authorization header return 401."""
        server = _make_server()
        app = build_http_app(server, auth_token="secret123")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            )
            self.assertEqual(resp.status, 401)

    async def test_bad_bearer_returns_401(self) -> None:
        """Requests with an incorrect bearer token return 401."""
        server = _make_server()
        app = build_http_app(server, auth_token="secret123")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                headers={"Authorization": "Bearer wrong"},
            )
            self.assertEqual(resp.status, 401)

    async def test_healthz_no_auth_required(self) -> None:
        """Healthz endpoint is accessible without auth."""
        server = _make_server()
        app = build_http_app(server, auth_token="secret123")
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            self.assertEqual(resp.status, 200)


# ---- Rate limiting middleware ----------------------------------------------


class TestRateLimitMiddleware(unittest.IsolatedAsyncioTestCase):
    """Tests for per-IP rate limiting middleware."""

    async def test_under_limit_passes(self) -> None:
        """Requests under the rate limit pass through."""
        server = _make_server()
        app = build_http_app(
            server, auth_token=None, rate_limit=10, rate_burst=10,
        )
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            )
            self.assertNotEqual(resp.status, 429)

    async def test_over_limit_returns_429(self) -> None:
        """Requests exceeding the rate limit return 429."""
        server = _make_server()
        app = build_http_app(
            server, auth_token=None, rate_limit=2, rate_burst=10,
        )
        async with TestClient(TestServer(app)) as client:
            # Use up the limit
            for _ in range(2):
                await client.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                )
            # Next request should be rate limited
            resp = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            )
            self.assertEqual(resp.status, 429)
            data = await resp.json()
            self.assertIn("retry_after", data)

    async def test_healthz_exempt_from_rate_limit(self) -> None:
        """Healthz endpoint is not rate limited."""
        server = _make_server()
        app = build_http_app(
            server, auth_token=None, rate_limit=1, rate_burst=1,
        )
        async with TestClient(TestServer(app)) as client:
            # Use up the limit on /mcp
            await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            )
            # /healthz should still work
            resp = await client.get("/healthz")
            self.assertEqual(resp.status, 200)


# ---- Healthz endpoint ------------------------------------------------------


class TestHealthzEndpoint(unittest.IsolatedAsyncioTestCase):
    """Tests for the /healthz endpoint."""

    async def test_returns_ok_with_tool_count(self) -> None:
        """Healthz returns ok status and tool count."""
        server = _make_server()
        app = build_http_app(server, auth_token=None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertTrue(data["ok"])
            self.assertGreaterEqual(data["tools"], 1)

    async def test_reports_auth_enabled(self) -> None:
        """Healthz reports auth_enabled when token is set."""
        server = _make_server()
        app = build_http_app(server, auth_token="secret")
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            data = await resp.json()
            self.assertTrue(data["auth_enabled"])

    async def test_reports_auth_disabled(self) -> None:
        """Healthz reports auth_enabled=False when no token."""
        server = _make_server()
        app = build_http_app(server, auth_token=None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            data = await resp.json()
            self.assertFalse(data["auth_enabled"])

    async def test_reports_rate_limit_config(self) -> None:
        """Healthz reports the configured rate limit values."""
        server = _make_server()
        app = build_http_app(
            server, auth_token=None, rate_limit=50, rate_burst=10,
        )
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            data = await resp.json()
            self.assertEqual(data["rate_limit"], 50)
            self.assertEqual(data["rate_burst"], 10)

    async def test_db_not_configured_without_pool(self) -> None:
        """Healthz reports db=not_configured when no pool is provided."""
        server = _make_server()
        app = build_http_app(server, auth_token=None, db_pool=None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            data = await resp.json()
            self.assertEqual(data["db"], "not_configured")

    async def test_db_connected_with_pool(self) -> None:
        """Healthz reports db=connected when pool is healthy."""
        server = _make_server()
        mock_pool = _MockPool()
        app = build_http_app(server, auth_token=None, db_pool=mock_pool)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            data = await resp.json()
            self.assertEqual(data["db"], "connected")

    async def test_db_error_when_pool_fails(self) -> None:
        """Healthz reports db=error when pool connection fails."""
        server = _make_server()
        mock_pool = _MockPool(fail=True)
        app = build_http_app(server, auth_token=None, db_pool=mock_pool)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            data = await resp.json()
            self.assertIn("error", data["db"])

    async def test_reports_uptime(self) -> None:
        """Healthz reports uptime_seconds >= 0."""
        server = _make_server()
        app = build_http_app(server, auth_token=None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            data = await resp.json()
            self.assertGreaterEqual(data["uptime_seconds"], 0)

    async def test_db_in_memory_without_pool_but_with_invocations(self) -> None:
        """Healthz reports db=in_memory when no db_pool but invocations exist."""
        from shared.mcp.memory_pool import InMemoryMcpPool
        from shared.mcp.store import InvocationStore

        reg = ToolRegistry()
        ping_tool, ping_handler = build_ping_tool()
        reg.register(ping_tool, ping_handler)
        pool = InMemoryMcpPool()
        server = McpServer(
            reg,
            invocation_store=InvocationStore(pool),
        )
        app = build_http_app(server, auth_token=None, db_pool=None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            data = await resp.json()
            self.assertEqual(data["db"], "in_memory")


if __name__ == "__main__":
    unittest.main()
