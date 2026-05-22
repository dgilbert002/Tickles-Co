"""
Module: test_cron_canary
Purpose: Smoke tests for cron health canary
Location: /opt/tickles/shared/tests/test_cron_canary.py
"""

import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone, timedelta
from shared.intelligence.cron_canary import CronCanary

@pytest.mark.asyncio
async def test_check_heartbeats_alerts_on_stale():
    # Setup mocks
    with patch("shared.intelligence.cron_canary.DatabasePool.get_instance") as mock_get_pool, \
         patch("shared.intelligence.cron_canary.sender_from_env") as mock_sender_factory, \
         patch("os.environ.get") as mock_env_get:
        
        mock_env_get.return_value = "123456" # TICKLES_ADMIN_CHAT_ID
        
        mock_telegram = AsyncMock()
        mock_sender_factory.return_value = mock_telegram
        
        mock_pool = AsyncMock()
        mock_get_pool.return_value = mock_pool
        
        # Mock database rows
        # One stale, one fresh
        now = datetime.now(timezone.utc)
        mock_pool.fetch_all.return_value = [
            {
                "agent_id": "stale_agent",
                "last_heartbeat_at": now - timedelta(seconds=1000),
                "status": "OK",
                "expected_interval_seconds": 60,
                "consecutive_failures": 0
            },
            {
                "agent_id": "fresh_agent",
                "last_heartbeat_at": now - timedelta(seconds=10),
                "status": "OK",
                "expected_interval_seconds": 60,
                "consecutive_failures": 0
            }
        ]
        
        canary = CronCanary()
        await canary.check_heartbeats()
        
        # Should alert for stale_agent
        mock_telegram.send_alert.assert_called_once()
        args, kwargs = mock_telegram.send_alert.call_args
        assert args[0] == "123456"
        assert "stale_agent" in args[1]
        assert "fresh_agent" not in args[1]
