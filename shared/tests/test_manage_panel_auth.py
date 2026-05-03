"""Module: test_manage_panel_auth
Purpose: Tests for default-deny auth middleware and session cookie handling.
Location: /opt/tickles/shared/tests/test_manage_panel_auth.py
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from shared.dashboard.server import (
    ALLOWED_PUBLIC,
    ALLOWED_PUBLIC_PREFIX,
    SESSION_COOKIE,
    auth_middleware,
    build_app,
)
from shared.dashboard import InMemoryDashboardPool
from shared.dashboard.store import (
    DashboardSessionStore,
    DashboardUserStore,
)
from shared.dashboard.auth import AuthConfig, DashboardAuth
from shared.dashboard.telegram import NullTelegramSender
from shared.intelligence.manage_panel.server_routes import attach_routes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_auth():
    pool = InMemoryDashboardPool()
    auth = DashboardAuth(
        users=DashboardUserStore(pool),
        otps=__import__("shared.dashboard.store", fromlist=["DashboardOtpStore"]).DashboardOtpStore(pool),
        sessions=DashboardSessionStore(pool),
        sender=NullTelegramSender(path="/dev/null"),
        config=AuthConfig(),
    )
    return pool, auth


async def _mk_client():
    pool, auth = _fresh_auth()
    await DashboardUserStore(pool).upsert(
        __import__("shared.dashboard.store", fromlist=["DashboardUser"]).DashboardUser(id=None, chat_id="42"),
    )
    from shared.dashboard.snapshot import SnapshotProviders
    providers = SnapshotProviders()
    app = build_app(auth, providers, expose_otp=True)
    attach_routes(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client, auth


async def _auth_token(client: TestClient) -> str:
    """Request OTP, verify it, and return the bearer token."""
    r1 = await client.post("/api/auth/request-otp", json={"chat_id": "42"})
    assert r1.status == 200
    code = (await r1.json())["code"]
    r2 = await client.post("/api/auth/verify-otp", json={"chat_id": "42", "code": code})
    assert r2.status == 200
    return (await r2.json())["token"]


# ---------------------------------------------------------------------------
# Public path tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_public_paths_are_accessible_without_auth():
    """All paths in ALLOWED_PUBLIC and ALLOWED_PUBLIC_PREFIX must be accessible without auth."""
    client, _ = await _mk_client()
    try:
        for path in ALLOWED_PUBLIC:
            if path in ("/", "/login"):
                # These may 404 if no index.html, that's fine — just not 401
                r = await client.get(path)
                assert r.status != 401, f"{path} should not require auth"
            elif path in ("/api/auth/request-otp", "/api/auth/verify-otp", "/api/auth/logout"):
                # POST-only endpoints return 405 on GET — also acceptable
                r = await client.get(path)
                assert r.status in (200, 404, 405), f"{path} returned {r.status}"
            else:
                r = await client.get(path)
                assert r.status in (200, 404), f"{path} returned {r.status}"

        # Static prefix
        r = await client.get("/static/test.css")
        assert r.status in (200, 404), f"/static/ returned {r.status}"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_private_paths_require_auth():
    """Paths not in ALLOWED_PUBLIC must return 401 without auth."""
    client, _ = await _mk_client()
    try:
        r = await client.get("/api/snapshot")
        assert r.status == 401
        body = await r.json()
        assert body["error"] == "auth required"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_manage_paths_require_auth():
    """/manage/* routes require authentication.

    HTML paths redirect to /login (302); API paths return 401 JSON.
    """
    client, _ = await _mk_client()
    try:
        # HTML path → redirect to login
        r = await client.get("/manage/sources", allow_redirects=False)
        assert r.status in (302, 307), f"Expected redirect, got {r.status}"
        assert "/login" in r.headers.get("Location", "")

        # API path → 401 JSON
        r2 = await client.post("/manage/api/sources/disable", json={"source_id": 1})
        assert r2.status == 401
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Session cookie tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_verify_otp_sets_session_cookie():
    """Successful OTP verification sets __Host-session cookie."""
    client, _ = await _mk_client()
    try:
        r1 = await client.post("/api/auth/request-otp", json={"chat_id": "42"})
        assert r1.status == 200
        code = (await r1.json())["code"]

        r2 = await client.post("/api/auth/verify-otp", json={"chat_id": "42", "code": code})
        assert r2.status == 200

        cookies = r2.cookies
        assert SESSION_COOKIE in cookies
        cookie = cookies[SESSION_COOKIE]
        assert cookie["secure"] is True
        assert cookie["httponly"] is True
        assert cookie.get("samesite") == "Strict"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_session_cookie_is_accepted_for_auth():
    """Requests with valid __Host-session cookie pass auth middleware.

    Note: TestClient runs over HTTP, so secure cookies are not sent
    automatically by the cookie jar. We manually inject the Cookie
    header to verify the middleware logic.
    """
    client, _ = await _mk_client()
    try:
        token = await _auth_token(client)

        # Manually send cookie header (bypasses secure-flag check in test)
        r3 = await client.get(
            "/api/snapshot",
            headers={"Cookie": f"{SESSION_COOKIE}={token}"},
        )
        assert r3.status == 200, "Session cookie should authenticate"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_invalid_session_cookie_returns_401():
    """A forged or expired session cookie is rejected."""
    client, _ = await _mk_client()
    try:
        r = await client.get(
            "/api/snapshot",
            headers={"Cookie": f"{SESSION_COOKIE}=forged-token"},
        )
        assert r.status == 401
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Bearer token still works
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_bearer_token_still_works():
    """Authorization: Bearer header continues to authenticate."""
    client, _ = await _mk_client()
    try:
        token = await _auth_token(client)

        r3 = await client.get(
            "/api/snapshot",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r3.status == 200
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Query string token fallback
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_query_string_token_fallback():
    """?token= query parameter is accepted as auth fallback."""
    client, _ = await _mk_client()
    try:
        token = await _auth_token(client)

        r3 = await client.get(f"/api/snapshot?token={token}")
        assert r3.status == 200
    finally:
        await client.close()
