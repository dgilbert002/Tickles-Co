"""Module: test_manage_csrf
Purpose: Tests for CSRF token issuance and validation.
Location: /opt/tickles/shared/tests/test_manage_csrf.py
"""
from __future__ import annotations

import secrets
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from shared.dashboard.csrf import CSRF_COOKIE, CSRF_HEADER, csrf_required, issue_csrf


# ---------------------------------------------------------------------------
# issue_csrf
# ---------------------------------------------------------------------------

def test_issue_csrf_sets_cookie():
    """issue_csrf sets the __Host-csrf cookie with correct flags."""
    response = web.Response(text="ok")
    token = issue_csrf(response)
    assert token
    assert len(token) > 20

    cookies = response.cookies
    assert CSRF_COOKIE in cookies
    cookie = cookies[CSRF_COOKIE]
    assert cookie["secure"] is True
    assert cookie.get("samesite") == "Strict"
    assert cookie["path"] == "/"
    # httponly=False so JS can read it
    assert cookie.get("httponly") is None or cookie.get("httponly") is False


def test_issue_csrf_returns_unique_tokens():
    """Each call to issue_csrf generates a unique token."""
    r1 = web.Response(text="ok")
    r2 = web.Response(text="ok")
    t1 = issue_csrf(r1)
    t2 = issue_csrf(r2)
    assert t1 != t2


# ---------------------------------------------------------------------------
# csrf_required decorator
# ---------------------------------------------------------------------------

async def _dummy_handler(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


@pytest.mark.anyio
async def test_csrf_get_passes_without_header():
    """GET requests pass through without CSRF check."""
    app = web.Application()
    app.router.add_get("/test", csrf_required(_dummy_handler))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        r = await client.get("/test")
        assert r.status == 200
    finally:
        await client.close()


@pytest.mark.anyio
async def test_csrf_post_without_header_returns_403():
    """POST without X-CSRF-Token header returns 403."""
    app = web.Application()
    app.router.add_post("/test", csrf_required(_dummy_handler))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        r = await client.post("/test", json={})
        assert r.status == 403
        body = await r.json()
        assert body["error"] == "csrf_mismatch"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_csrf_post_with_matching_header_passes():
    """POST with matching X-CSRF-Token header and cookie passes."""
    app = web.Application()
    app.router.add_post("/test", csrf_required(_dummy_handler))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        token = secrets.token_urlsafe(32)
        client.session.cookie_jar.update_cookies({CSRF_COOKIE: token})
        r = await client.post("/test", json={}, headers={CSRF_HEADER: token})
        assert r.status == 200
    finally:
        await client.close()


@pytest.mark.anyio
async def test_csrf_post_with_mismatched_header_returns_403():
    """POST with mismatched X-CSRF-Token header returns 403."""
    app = web.Application()
    app.router.add_post("/test", csrf_required(_dummy_handler))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        client.session.cookie_jar.update_cookies({CSRF_COOKIE: "real-token"})
        r = await client.post("/test", json={}, headers={CSRF_HEADER: "fake-token"})
        assert r.status == 403
    finally:
        await client.close()


@pytest.mark.anyio
async def test_csrf_post_with_cookie_but_no_header_returns_403():
    """POST with cookie but missing header returns 403."""
    app = web.Application()
    app.router.add_post("/test", csrf_required(_dummy_handler))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        client.session.cookie_jar.update_cookies({CSRF_COOKIE: "token"})
        r = await client.post("/test", json={})
        assert r.status == 403
    finally:
        await client.close()


@pytest.mark.anyio
async def test_csrf_head_passes_without_header():
    """HEAD requests pass through without CSRF check."""
    app = web.Application()
    app.router.add_head("/test", csrf_required(_dummy_handler))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        r = await client.head("/test")
        assert r.status == 200
    finally:
        await client.close()
