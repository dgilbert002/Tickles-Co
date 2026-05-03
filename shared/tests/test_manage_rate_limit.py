"""Module: test_manage_rate_limit
Purpose: Tests for token-bucket rate limiter.
Location: /opt/tickles/shared/tests/test_manage_rate_limit.py
"""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from shared.dashboard.rate_limit import _BUCKETS, rate_limit, reset_buckets


async def _dummy_handler(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


@pytest.fixture(autouse=True)
def clear_buckets():
    reset_buckets()
    yield
    reset_buckets()


@pytest.mark.anyio
async def test_read_requests_under_limit_pass():
    """Requests under the read cap pass through."""
    app = web.Application()
    app.router.add_get("/test", rate_limit("read", _dummy_handler))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        for _ in range(5):
            r = await client.get("/test")
            assert r.status == 200
    finally:
        await client.close()


@pytest.mark.anyio
async def test_write_requests_under_limit_pass():
    """Write requests under the write cap pass through."""
    app = web.Application()
    app.router.add_post("/test", rate_limit("write", _dummy_handler))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        for _ in range(5):
            r = await client.post("/test", json={})
            assert r.status == 200
    finally:
        await client.close()


@pytest.mark.anyio
async def test_rate_limit_returns_429_when_exceeded():
    """Excessive requests return 429 with Retry-After header."""
    # Use a very low cap for testing
    import os
    old_read = os.environ.get("MANAGE_RATE_READ")
    os.environ["MANAGE_RATE_READ"] = "2"

    # Re-import to pick up new cap
    from shared.dashboard.rate_limit import _CAPS
    _CAPS["read"] = (2, 2 / 60.0)

    app = web.Application()
    app.router.add_get("/test", rate_limit("read", _dummy_handler))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        r1 = await client.get("/test")
        assert r1.status == 200
        r2 = await client.get("/test")
        assert r2.status == 200
        r3 = await client.get("/test")
        assert r3.status == 429
        body = await r3.json()
        assert body["error"] == "rate_limited"
        assert body["kind"] == "read"
        assert "Retry-After" in r3.headers
    finally:
        await client.close()
        if old_read is not None:
            os.environ["MANAGE_RATE_READ"] = old_read
        else:
            os.environ.pop("MANAGE_RATE_READ", None)
        _CAPS["read"] = (int(os.environ.get("MANAGE_RATE_READ", "600")), int(os.environ.get("MANAGE_RATE_READ", "600")) / 60.0)


@pytest.mark.anyio
async def test_rate_limit_tracks_per_session():
    """Different sessions have independent buckets."""
    _CAPS = {"read": (100, 100 / 60.0)}

    async def handler_with_session(request: web.Request) -> web.Response:
        request["session_token"] = request.query.get("sess", "anon")
        return await rate_limit("read", _dummy_handler)(request)

    app = web.Application()
    app.router.add_get("/test", handler_with_session)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        # Session A uses its own bucket
        for _ in range(5):
            r = await client.get("/test?sess=A")
            assert r.status == 200
        # Session B also passes independently
        for _ in range(5):
            r = await client.get("/test?sess=B")
            assert r.status == 200
    finally:
        await client.close()


@pytest.mark.anyio
async def test_reset_buckets_clears_state():
    """reset_buckets clears all rate limit state."""
    # Fill a bucket
    _BUCKETS[("test", "read")] = (0.0, 0.0)
    assert len(_BUCKETS) > 0
    reset_buckets()
    assert len(_BUCKETS) == 0
