"""
Test: shared/tests/test_dashboard_drill.py
Purpose: Verify Phase L.3 Trader Drill and Chart Renderer functionality.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from shared.dashboard.server import build_app
from shared.dashboard.snapshot import SnapshotProviders
import shared.dashboard.chart_renderer

@pytest.fixture
def mock_auth():
    auth = MagicMock()
    auth.authenticate_token = AsyncMock(return_value=({"id": 1, "chat_id": "123"}, {"id": "sess_1"}))
    return auth

@pytest.fixture
def mock_providers():
    return SnapshotProviders()

async def test_trader_drill_api(mock_auth, mock_providers):
    app = build_app(mock_auth, mock_providers)
    client = MagicMock() # We'll use manual mocks for the snapshot functions
    
    mock_drill_data = {
        "ok": True,
        "profile": {"id": 1, "handle_raw": "test_trader"},
        "trades": [{"id": 101, "symbol": "BTCUSDT", "realized_pnl": 50.0}],
        "performance": [{"period": "all", "accuracy": 0.75}],
        "signals": []
    }
    
    with patch("shared.dashboard.snapshot.get_trader_drill_data", AsyncMock(return_value=mock_drill_data)):
        # Simulate request
        request = MagicMock(spec=web.Request)
        request.query = {"trader_id": "1"}
        request.app = app
        
        from shared.dashboard.server import handle_trader_drill
        response = await handle_trader_drill(request)
        
        assert response.status == 200
        body = json.loads(response.body)
        assert body["ok"] is True
        assert body["profile"]["handle_raw"] == "test_trader"

async def test_chart_render_api(mock_auth, mock_providers, tmp_path):
    app = build_app(mock_auth, mock_providers)
    
    mock_interp = {
        "id": 500,
        "llm_levels": {"entry": "50000"},
        "local_path": str(tmp_path / "orig.png"),
        "prompt_version": "v1"
    }
    
    # Create a dummy image
    from PIL import Image
    img = Image.new('RGB', (100, 100), color = 'red')
    img.save(tmp_path / "orig.png")
    
    with patch("shared.dashboard.snapshot.get_interpretation_by_id", AsyncMock(return_value=mock_interp)):
        with patch("shared.dashboard.chart_renderer.CHART_DIR", tmp_path / "charts"):
            # Simulate request
            request = MagicMock(spec=web.Request)
            request.match_info = {"interp_id": "500"}
            request.app = app
            
            from shared.dashboard.server import handle_chart
            response = await handle_chart(request)
            
            # handle_chart returns a FileResponse which is hard to assert on without a real server
            # but we can check if it tried to render
            assert response is not None
            assert (tmp_path / "charts").exists()
            # Check if an SVG was created
            svgs = list((tmp_path / "charts").glob("*.svg"))
            assert len(svgs) > 0

if __name__ == "__main__":
    # Manual run if pytest not configured
    async def run_tests():
        print("Running Trader Drill tests...")
        m_auth = MagicMock()
        m_auth.authenticate_token = AsyncMock(return_value=({"id": 1}, {"id": "s1"}))
        m_prov = SnapshotProviders()
        
        await test_trader_drill_api(m_auth, m_prov)
        print("Trader Drill API OK")
        
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path
            await test_chart_render_api(m_auth, m_prov, Path(tmp))
        print("Chart Render API OK")

    asyncio.run(run_tests())
