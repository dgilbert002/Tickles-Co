"""
Module: test_sessions
Purpose: Smoke tests for the Time/Sessions Service.
Location: /opt/tickles/shared/tests/test_sessions.py
"""

import unittest
from datetime import datetime, time, timezone
from unittest.mock import AsyncMock, MagicMock
from shared.utils.sessions import SessionService, TradingSession, SessionDefinition

class TestSessions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = MagicMock()
        self.pool.fetch_all = AsyncMock()
        self.pool.fetch_one = AsyncMock()

    async def test_session_service_load_and_cache(self):
        # Mock database responses
        self.pool.fetch_all.side_effect = [
            [{"id": 1, "name": "crypto_24_7", "timezone": "UTC"}], # sessions
            [{"day_of_week": 0, "open_time": time(0, 0), "close_time": time(23, 59)}] # definitions
        ]
        
        service = SessionService(self.pool)
        await service.load_sessions()
        
        session = await service.get_session("crypto_24_7")
        self.assertEqual(session.name, "crypto_24_7")
        self.assertEqual(session.tz.key, "UTC")
        self.assertIn(0, session.schedule)

    async def test_trading_session_is_open(self):
        # Crypto 24/7
        defns = [SessionDefinition(i, time(0, 0), time(23, 59)) for i in range(7)]
        session = TradingSession("crypto", "UTC", defns)
        
        # Monday 10:00 UTC
        dt = datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc)
        self.assertTrue(session.is_open(dt))
        
        # London Equity (Mon-Fri 08:00-16:30 Europe/London)
        # 2026-04-20 is a Monday. BST is UTC+1.
        # 08:00 London = 07:00 UTC
        # 16:30 London = 15:30 UTC
        london_defns = [SessionDefinition(i, time(8, 0), time(16, 30)) for i in range(5)]
        london = TradingSession("london", "Europe/London", london_defns)
        
        # Monday 06:00 UTC (07:00 London) -> Closed
        self.assertFalse(london.is_open(datetime(2026, 4, 20, 6, 0, tzinfo=timezone.utc)))
        
        # Monday 08:00 UTC (09:00 London) -> Open
        self.assertTrue(london.is_open(datetime(2026, 4, 20, 8, 0, tzinfo=timezone.utc)))
        
        # Saturday -> Closed
        self.assertFalse(london.is_open(datetime(2026, 4, 25, 10, 0, tzinfo=timezone.utc)))

    async def test_trading_session_next_close(self):
        london_defns = [SessionDefinition(i, time(8, 0), time(16, 30)) for i in range(5)]
        london = TradingSession("london", "Europe/London", london_defns)
        
        # Monday 08:00 UTC (09:00 London)
        # Next close should be today at 16:30 London (15:30 UTC)
        dt = datetime(2026, 4, 20, 8, 0, tzinfo=timezone.utc)
        next_close = london.get_next_close(dt)
        self.assertEqual(next_close, datetime(2026, 4, 20, 15, 30, tzinfo=timezone.utc))
        
        # Friday 20:00 UTC (21:00 London)
        # Next close should be next Monday at 16:30 London
        dt = datetime(2026, 4, 24, 20, 0, tzinfo=timezone.utc)
        next_close = london.get_next_close(dt)
        # 2026-04-27 is the next Monday
        self.assertEqual(next_close, datetime(2026, 4, 27, 15, 30, tzinfo=timezone.utc))

if __name__ == "__main__":
    unittest.main()
