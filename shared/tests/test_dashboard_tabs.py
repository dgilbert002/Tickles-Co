"""
Module: test_dashboard_tabs
Purpose: Verify Phase L.2 read-only tab API endpoints.
Location: /opt/tickles/shared/tests/test_dashboard_tabs.py
"""

import asyncio
import pytest
from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import AsyncMock, MagicMock, patch

from shared.dashboard.server import build_app
from shared.dashboard.snapshot import SnapshotProviders

async def _run_test(test_fn):
    auth = MagicMock()
    auth.authenticate_token = AsyncMock(return_value=(MagicMock(), MagicMock()))
    
    providers = SnapshotProviders()
    app = build_app(auth, providers)
    
    # Round 12 (2026-05-24): snapshot builder now calls aggregate_live_positions
    # (was aggregate_open_positions). Legacy GET /api/positions route retired —
    # tests below use /api/positions/live instead.
    with patch("shared.dashboard.snapshot.get_overview_stats", new_callable=AsyncMock) as m_stats, \
         patch("shared.dashboard.snapshot.aggregate_live_positions", new_callable=AsyncMock) as m_pos, \
         patch("shared.dashboard.snapshot.aggregate_leaderboard", new_callable=AsyncMock) as m_lead, \
         patch("shared.dashboard.snapshot.aggregate_signals", new_callable=AsyncMock) as m_sig, \
         patch("shared.dashboard.snapshot.aggregate_interpretations", new_callable=AsyncMock) as m_int:
        
        m_stats.return_value = {"signals_today_count": 10}
        m_pos.return_value = [{"id": 1, "symbol": "BTC/USDT", "_company": "rubicon"}]
        m_lead.return_value = [{"actor_id": "trader1", "edge_score": 0.8, "_company": "rubicon"}]
        m_sig.return_value = [{"id": 101, "symbol": "ETH/USDT", "_company": "rubicon"}]
        m_int.return_value = [{"id": 201, "symbol": "SOL/USDT", "_company": "rubicon"}]
        
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            await test_fn(client)
        finally:
            await client.close()

def test_api_snapshot():
    async def _test(client):
        resp = await client.get("/api/snapshot", headers={"Authorization": "Bearer test-token"})
        assert resp.status == 200
        data = await resp.json()
        assert data["signals_today_count"] == 10
        assert len(data["positions"]) == 1
    asyncio.run(_run_test(_test))

def test_api_leaderboard():
    async def _test(client):
        resp = await client.get("/api/leaderboard", headers={"Authorization": "Bearer test-token"})
        assert resp.status == 200
        data = await resp.json()
        assert len(data["leaderboard"]) == 1
    asyncio.run(_run_test(_test))

def test_api_signals():
    async def _test(client):
        resp = await client.get("/api/signals", headers={"Authorization": "Bearer test-token"})
        assert resp.status == 200
        data = await resp.json()
        assert len(data["signals"]) == 1
    asyncio.run(_run_test(_test))

def test_api_positions_live():
    """Round 12 (2026-05-24): retargeted from legacy /api/positions to
    /api/positions/live. The legacy route was retired; the live endpoint
    serves the same use-case (live tracked + broker fills) without the
    7-day closed tail that nothing was rendering.

    The handler imports ``aggregate_live_positions`` lazily inside the
    function body to avoid module-load-order coupling, so we patch the
    canonical name in ``shared.dashboard.snapshot`` (where it's defined)
    rather than ``server`` (where it's imported on demand)."""
    async def _test(client):
        with patch(
            "shared.dashboard.snapshot.aggregate_live_positions",
            new_callable=AsyncMock,
        ) as m_live:
            m_live.return_value = [{"id": 1, "symbol": "BTC/USDT", "_company": "rubicon"}]
            resp = await client.get(
                "/api/positions/live",
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert len(data["positions"]) == 1
    asyncio.run(_run_test(_test))

def test_api_interpretations():
    async def _test(client):
        resp = await client.get("/api/interpretations", headers={"Authorization": "Bearer test-token"})
        assert resp.status == 200
        data = await resp.json()
        assert len(data["interpretations"]) == 1
    asyncio.run(_run_test(_test))
