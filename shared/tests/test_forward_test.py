"""
Smoke test for ForwardTestEngine.
Location: /opt/tickles/shared/tests/test_forward_test.py
"""

import unittest
from unittest.mock import MagicMock, patch
import pandas as pd
import numpy as np
from datetime import datetime, timezone

from shared.backtest.forward_test import ForwardTestEngine
from shared.backtest.engine import BacktestConfig

class TestForwardTestEngine(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Patch ClickHouseWriter to avoid network calls
        with patch("shared.backtest.forward_test.ClickHouseWriter"):
            self.engine = ForwardTestEngine()
            self.engine.ch = MagicMock()

    @patch("shared.backtest.forward_test.get_strategy")
    async def test_forward_test_parity_loop(self, mock_get_strat):
        # 1. Setup strategy mock: always return 1.0 (long)
        mock_strat = MagicMock(return_value=pd.Series([1.0], index=[0]))
        mock_get_strat.return_value = mock_strat

        cfg = BacktestConfig(
            symbol="BTC/USDT",
            source="binance",
            timeframe="1h",
            strategy_name="test_strat",
            initial_capital=10000.0,
            start_date="2026-01-01",
            end_date="2026-01-02"
        )
        
        run_id = await self.engine.start_run("strat_123", cfg)
        self.assertIn(run_id, self.engine.executors)

        # 2. First candle arrives (Bar T)
        # Signal is generated at end of Bar T, but NOT applied yet (Rule 1 parity)
        candle1 = {
            "snapshotTime": "2026-04-21T12:00:00Z",
            "openPrice": 50000.0,
            "highPrice": 50500.0,
            "lowPrice": 49900.0,
            "closePrice": 50100.0
        }
        await self.engine.on_candle("BTC/USDT", "1h", candle1)
        
        executor = self.engine.executors[run_id]
        self.assertEqual(executor.position, 0.0)
        self.assertTrue(hasattr(executor, "_pending_signal"))
        self.assertEqual(executor._pending_signal["sig"], 1.0)

        # 3. Second candle arrives (Bar T+1)
        # Signal from Bar T is applied at Bar T+1 Open
        candle2 = {
            "snapshotTime": "2026-04-21T13:00:00Z",
            "openPrice": 50150.0,
            "highPrice": 51000.0,
            "lowPrice": 50100.0,
            "closePrice": 50800.0
        }
        await self.engine.on_candle("BTC/USDT", "1h", candle2)
        
        self.assertNotEqual(executor.position, 0.0)
        # 50150.0 is candle2 openPrice. executor.slip_rate is 2 bps (0.0002).
        # 50150 * 1.0002 = 50160.03
        self.assertAlmostEqual(executor.entry_px, 50160.03, places=2)
        self.assertEqual(executor.entry_at.hour, 13) # Filled at T+1 open (candle2 is 13:00)

    async def test_persist_trades(self):
        cfg = BacktestConfig(
            symbol="BTC/USDT",
            source="binance",
            timeframe="1h",
            initial_capital=10000.0,
            stop_loss_pct=1.0,
            start_date="2026-01-01",
            end_date="2026-01-02"
        )
        run_id = await self.engine.start_run("strat_123", cfg)
        executor = self.engine.executors[run_id]
        
        # Manually trigger a trade close
        executor.position = 1.0
        executor.entry_px = 50000.0
        executor.entry_at = pd.Timestamp.now(tz="UTC")
        executor.entry_direction = "long"
        
        # SL hit
        ts = np.datetime64("2026-04-21T14:00:00")
        executor.process_intrabar(ts, 50000.0, 50100.0, 49000.0, 49500.0)
        
        self.assertEqual(len(executor.trades), 1)
        self.engine._persist_new_trades(run_id)
        
        self.engine.ch.write_forward_trade.assert_called_once()
        self.assertEqual(self.engine.last_trade_counts[run_id], 1)

if __name__ == "__main__":
    unittest.main()
