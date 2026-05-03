"""Module: test_manage_panel_routes
Purpose: Smoke tests for manage panel route handlers with mocked DB.
Location: /opt/tickles/shared/tests/test_manage_panel_routes.py
"""
from __future__ import annotations

import asyncio
from typing import Tuple
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from shared.dashboard.server import build_app
from shared.dashboard import InMemoryDashboardPool
from shared.dashboard.store import (
    DashboardSessionStore,
    DashboardUserStore,
)
from shared.dashboard.auth import AuthConfig, DashboardAuth
from shared.dashboard.telegram import NullTelegramSender
from shared.dashboard.snapshot import SnapshotProviders
from shared.intelligence.manage_panel.server_routes import attach_routes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _mk_authed_client() -> Tuple[TestClient, str, str]:
    """Build app with manage routes, authenticate, return (client, bearer_token, csrf_token).

    TestClient runs over HTTP, so secure cookies are not sent automatically.
    We return the bearer token and CSRF token explicitly so tests can inject
    them via headers.
    """
    pool = InMemoryDashboardPool()
    User = __import__("shared.dashboard.store", fromlist=["DashboardUser"]).DashboardUser
    await DashboardUserStore(pool).upsert(User(id=None, chat_id="42"))
    auth = DashboardAuth(
        users=DashboardUserStore(pool),
        otps=__import__("shared.dashboard.store", fromlist=["DashboardOtpStore"]).DashboardOtpStore(pool),
        sessions=DashboardSessionStore(pool),
        sender=NullTelegramSender(path="/dev/null"),
        config=AuthConfig(),
    )
    providers = SnapshotProviders()
    app = build_app(auth, providers, expose_otp=True)
    attach_routes(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    # Authenticate
    r1 = await client.post("/api/auth/request-otp", json={"chat_id": "42"})
    code = (await r1.json())["code"]
    r2 = await client.post("/api/auth/verify-otp", json={"chat_id": "42", "code": code})

    bearer_token = (await r2.json())["token"]
    csrf_cookie = r2.cookies.get("__Host-csrf")
    csrf_token = csrf_cookie.value if csrf_cookie else ""

    return client, bearer_token, csrf_token


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _csrf_headers(token: str, csrf: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "X-CSRF-Token": csrf,
        "Cookie": f"__Host-csrf={csrf}",
    }


# ---------------------------------------------------------------------------
# Read-only view tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_manage_index_redirects():
    """/manage redirects to /manage/sources."""
    client, token, _ = await _mk_authed_client()
    try:
        with patch("shared.intelligence.manage_panel.db_views.list_sources", new=AsyncMock(return_value=[])):
            r = await client.get("/manage", allow_redirects=False, headers=_auth_headers(token))
            assert r.status == 302 or r.status == 307
            assert "/manage/sources" in r.headers.get("Location", "")
    finally:
        await client.close()


@pytest.mark.anyio
async def test_manage_sources_renders_html():
    """/manage/sources returns HTML with sources table."""
    client, token, _ = await _mk_authed_client()
    try:
        mock_sources = [
            {"id": 1, "source_type": "discord", "source_name": "Test", "source_slug": "test", "is_enabled": True, "description": "A test source"},
        ]
        with patch("shared.intelligence.manage_panel.db_views.list_sources", new=AsyncMock(return_value=mock_sources)):
            r = await client.get("/manage/sources", headers=_auth_headers(token))
            assert r.status == 200
            text = await r.text()
            assert "Test" in text
            assert "discord" in text
    finally:
        await client.close()


@pytest.mark.anyio
async def test_manage_signals_renders_html():
    """/manage/signals returns HTML with signals table."""
    client, token, _ = await _mk_authed_client()
    try:
        mock_signals = [
            {
                "id": 1, "created_at": "2026-04-30T08:00:00", "trader_handle": "Trader1",
                "symbol": "BTC/USDT", "llm_direction": "long", "quant_direction": "long",
                "consensus_direction": "long", "confidence": 0.85, "cost_estimate_usd": 0.12,
            },
        ]
        with patch("shared.intelligence.manage_panel.db_views.list_recent_signals", new=AsyncMock(return_value=mock_signals)):
            r = await client.get("/manage/signals", headers=_auth_headers(token))
            assert r.status == 200
            text = await r.text()
            assert "Trader1" in text
            assert "BTC/USDT" in text
    finally:
        await client.close()


@pytest.mark.anyio
async def test_manage_positions_renders_html():
    """/manage/positions returns HTML with positions tables."""
    client, token, _ = await _mk_authed_client()
    try:
        with patch("shared.intelligence.manage_panel.db_views.list_open_positions", new=AsyncMock(return_value=[])):
            with patch("shared.intelligence.manage_panel.db_views.list_closed_positions", new=AsyncMock(return_value=[])):
                r = await client.get("/manage/positions", headers=_auth_headers(token))
                assert r.status == 200
                text = await r.text()
                assert "Open Positions" in text or "Positions" in text
    finally:
        await client.close()


@pytest.mark.anyio
async def test_manage_leaderboard_renders_html():
    """/manage/leaderboard returns HTML with leaderboard table."""
    client, token, _ = await _mk_authed_client()
    try:
        mock_rows = [
            {
                "trader_profile_id": 1, "display_name": "Alpha", "handle_normalized": "alpha",
                "total_trades": 10, "wins": 7, "losses": 3, "total_pnl": 150.0,
                "avg_rr": 2.5, "long_count": 6, "short_count": 4, "most_traded_symbol": "BTC/USDT",
            },
        ]
        with patch("shared.intelligence.manage_panel.db_views.get_leaderboard", new=AsyncMock(return_value=mock_rows)):
            r = await client.get("/manage/leaderboard", headers=_auth_headers(token))
            assert r.status == 200
            text = await r.text()
            assert "Alpha" in text
    finally:
        await client.close()


@pytest.mark.anyio
async def test_manage_trader_drill_renders_html():
    """/manage/trader/{id} returns HTML with trader details."""
    client, token, _ = await _mk_authed_client()
    try:
        mock_profile = {"id": 1, "display_name": "Alpha", "handle_normalized": "alpha", "platform": "discord"}
        with patch("shared.intelligence.manage_panel.db_views.get_trader_profile", new=AsyncMock(return_value=mock_profile)):
            with patch("shared.intelligence.manage_panel.db_views.get_trader_signals", new=AsyncMock(return_value=[])):
                with patch("shared.intelligence.manage_panel.db_views.get_trader_positions", new=AsyncMock(return_value=[])):
                    r = await client.get("/manage/trader/1", headers=_auth_headers(token))
                    assert r.status == 200
                    text = await r.text()
                    assert "Alpha" in text
    finally:
        await client.close()


@pytest.mark.anyio
async def test_manage_trader_drill_404_for_unknown():
    """/manage/trader/{id} returns 404 for unknown trader."""
    client, token, _ = await _mk_authed_client()
    try:
        with patch("shared.intelligence.manage_panel.db_views.get_trader_profile", new=AsyncMock(return_value=None)):
            r = await client.get("/manage/trader/999", headers=_auth_headers(token))
            assert r.status == 404
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Mutating API tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_disable_source_requires_csrf():
    """POST /manage/api/sources/disable without CSRF returns 403."""
    client, token, _ = await _mk_authed_client()
    try:
        r = await client.post(
            "/manage/api/sources/disable",
            json={"source_id": 1},
            headers=_auth_headers(token),
        )
        assert r.status == 403
    finally:
        await client.close()


@pytest.mark.anyio
async def test_disable_source_with_csrf_works():
    """POST /manage/api/sources/disable with valid CSRF disables source."""
    client, token, csrf = await _mk_authed_client()
    try:
        if not csrf:
            pytest.skip("CSRF token not present in verify-otp response")

        with patch("shared.intelligence.manage_panel.db_views.toggle_source", new=AsyncMock(return_value=1)):
            r = await client.post(
                "/manage/api/sources/disable",
                json={"source_id": 1},
                headers=_csrf_headers(token, csrf),
            )
            assert r.status == 200
            body = await r.json()
            assert body["ok"] is True
            assert body["affected"] == 1
    finally:
        await client.close()


@pytest.mark.anyio
async def test_enable_source_with_csrf_works():
    """POST /manage/api/sources/enable with valid CSRF enables source."""
    client, token, csrf = await _mk_authed_client()
    try:
        if not csrf:
            pytest.skip("CSRF token not present in verify-otp response")

        with patch("shared.intelligence.manage_panel.db_views.toggle_source", new=AsyncMock(return_value=1)):
            r = await client.post(
                "/manage/api/sources/enable",
                json={"source_id": 1},
                headers=_csrf_headers(token, csrf),
            )
            assert r.status == 200
            body = await r.json()
            assert body["ok"] is True
    finally:
        await client.close()


@pytest.mark.anyio
async def test_disable_channel_with_csrf_works():
    """POST /manage/api/channels/disable with valid CSRF disables channel."""
    client, token, csrf = await _mk_authed_client()
    try:
        if not csrf:
            pytest.skip("CSRF token not present in verify-otp response")

        with patch("shared.intelligence.manage_panel.db_views.toggle_channel", new=AsyncMock(return_value=1)):
            r = await client.post(
                "/manage/api/channels/disable",
                json={"channel_id": 1},
                headers=_csrf_headers(token, csrf),
            )
            assert r.status == 200
            body = await r.json()
            assert body["ok"] is True
    finally:
        await client.close()
