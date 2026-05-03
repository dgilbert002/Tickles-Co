"""
Module: shared.tests.test_dashboard_ws
Purpose: Smoke test for the Live Queue WebSocket.
Location: /opt/tickles/shared/tests/test_dashboard_ws.py
"""

import pytest
import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock
from aiohttp import web

from shared.dashboard.server import build_app
from shared.dashboard.protocol import DashboardUser, DashboardSession
from shared.dashboard.auth import InvalidSession

@pytest.fixture
async def client(aiohttp_client):
    auth = MagicMock()
    auth.authenticate_token = AsyncMock(return_value=(
        DashboardUser(id=1, chat_id="123", role="owner"),
        DashboardSession(id=1, chat_id="123", token_hash="hash")
    ))
    
    providers = MagicMock()
    providers.services = AsyncMock()
    providers.services.list_services = AsyncMock(return_value=[])
    
    app = build_app(auth, providers)
    return await aiohttp_client(app)

@pytest.mark.asyncio
async def test_queue_ws_flow(client):
    """Verify that the WebSocket connects and receives data."""
    sample_data = {
        "news_pending": [{"id": 1, "source": "test", "headline": "hello", "collected_at": "2026-05-01T12:00:00Z"}],
        "media_pending": [],
        "positions_active": []
    }
    
    with patch("shared.dashboard.ws.get_queue_data", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = sample_data
        
        # Connect with token
        async with client.ws_connect("/dashboard/ws/queue?token=valid_token") as ws:
            # Receive first message
            msg = await ws.receive_json()
            assert msg["type"] == "queue_update"
            assert msg["data"]["news_pending"][0]["id"] == 1
            
            # Verify it called get_queue_data
            mock_get.assert_called_once()

@pytest.mark.asyncio
async def test_queue_ws_max_clients(client):
    """Verify that the server rejects connections beyond MAX_WS_CLIENTS."""
    # We must patch the active_clients in the module where it's used
    import shared.dashboard.ws as ws_mod
    
    # Clear and fill up clients
    ws_mod.active_clients.clear()
    # We need to make sure we are filling the EXACT set used by the handler
    for i in range(ws_mod.MAX_WS_CLIENTS):
        ws_mod.active_clients.add(MagicMock())
        
    try:
        # Use ws_connect to simulate real usage
        # We use a try/except block because ws_connect raises for non-101
        try:
            async with client.ws_connect("/dashboard/ws/queue?token=valid_token") as ws:
                pytest.fail("Should have failed to connect")
        except Exception as e:
            # aiohttp raises WSServerHandshakeError for non-101
            from aiohttp import WSServerHandshakeError
            if not isinstance(e, WSServerHandshakeError):
                # If it's not the handshake error, maybe it's a Failed from pytest.fail
                raise
            assert e.status == 503
    finally:
        ws_mod.active_clients.clear()

@pytest.mark.skip(
    reason=(
        "auth_middleware was deliberately disabled in shared/dashboard/server.py "
        "(2026-05-02, per user instruction to remove the Telegram sign-in gate). "
        "This test asserts middleware-enforced 401/302 redirects which no longer "
        "apply. If auth is re-enabled, restore the assertions and unskip."
    )
)
@pytest.mark.asyncio
async def test_queue_ws_unauthorized(client):
    """Verify that unauthorized WS connections are rejected by middleware.

    Skipped: dashboard auth_middleware is currently a pass-through that
    attaches a fake dev user; rejection no longer happens at the
    middleware layer. See server.py auth_middleware docstring.
    """
    # 1. Token present but invalid -> 401
    client.app["_auth"].authenticate_token.side_effect = InvalidSession("Invalid token")
    resp = await client.get("/dashboard/ws/queue?token=invalid")
    assert resp.status == 401

    # 2. Token missing -> 302 Redirect to /login (since it's not an /api/ path)
    client.app["_auth"].authenticate_token.side_effect = None  # Reset
    # MUST use allow_redirects=False to see the 302
    resp = await client.get("/dashboard/ws/queue", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "login"
