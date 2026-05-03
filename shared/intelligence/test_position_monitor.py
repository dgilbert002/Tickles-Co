"""
Module: test_position_monitor
Purpose: Smoke tests for PositionMonitor daemon (no DB required).
Location: /opt/tickles/shared/intelligence/test_position_monitor.py
"""

import unittest
from datetime import datetime, timezone

from shared.intelligence.position_monitor import (
    MonitorConfig,
    PositionMonitor,
    _build_snapshot,
)
from shared.intelligence.position_quant import compute_pnl


class TestMonitorConfig(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = MonitorConfig()
        self.assertEqual(cfg.poll_interval_s, 60.0)
        self.assertEqual(cfg.batch_size, 50)
        self.assertEqual(cfg.agent_opinion_every_n, 10)
        self.assertEqual(cfg.default_timeframe, "1m")


class TestBuildSnapshot(unittest.TestCase):
    def test_long_open(self) -> None:
        position = {
            "id": 1,
            "direction": "long",
            "entry_price": 100.0,
            "position_size": 1.0,
            "leverage": 1.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "created_at": datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc),
        }
        now = datetime(2026, 4, 26, 13, 0, 0, tzinfo=timezone.utc)
        snapshot = _build_snapshot(position, 105.0, now)

        self.assertEqual(snapshot.position_id, 1)
        self.assertEqual(snapshot.current_price, 105.0)
        self.assertAlmostEqual(snapshot.unrealized_pnl, 5.0)
        self.assertAlmostEqual(snapshot.pnl_pct, 0.05)
        self.assertAlmostEqual(snapshot.distance_to_tp, 5.0)
        self.assertAlmostEqual(snapshot.hours_open, 1.0)
        self.assertFalse(snapshot.sl_hit)
        self.assertFalse(snapshot.tp_hit)

    def test_long_sl_hit(self) -> None:
        position = {
            "id": 2,
            "direction": "long",
            "entry_price": 100.0,
            "position_size": 1.0,
            "leverage": 1.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "created_at": datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc),
        }
        now = datetime(2026, 4, 26, 13, 0, 0, tzinfo=timezone.utc)
        snapshot = _build_snapshot(position, 94.0, now)

        self.assertTrue(snapshot.sl_hit)
        self.assertFalse(snapshot.tp_hit)
        self.assertAlmostEqual(snapshot.unrealized_pnl, -6.0)

    def test_short_tp_hit(self) -> None:
        position = {
            "id": 3,
            "direction": "short",
            "entry_price": 100.0,
            "position_size": 1.0,
            "leverage": 1.0,
            "stop_loss": 105.0,
            "take_profit": 90.0,
            "created_at": datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc),
        }
        now = datetime(2026, 4, 26, 13, 0, 0, tzinfo=timezone.utc)
        snapshot = _build_snapshot(position, 89.0, now)

        self.assertFalse(snapshot.sl_hit)
        self.assertTrue(snapshot.tp_hit)
        self.assertAlmostEqual(snapshot.unrealized_pnl, 11.0)

    def test_no_sl_tp(self) -> None:
        position = {
            "id": 4,
            "direction": "long",
            "entry_price": 100.0,
            "position_size": 1.0,
            "leverage": 1.0,
            "stop_loss": None,
            "take_profit": None,
            "created_at": datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc),
        }
        now = datetime(2026, 4, 26, 13, 0, 0, tzinfo=timezone.utc)
        snapshot = _build_snapshot(position, 105.0, now)

        self.assertIsNone(snapshot.distance_to_sl)
        self.assertIsNone(snapshot.distance_to_tp)
        self.assertIsNone(snapshot.rr_ratio)
        self.assertFalse(snapshot.sl_hit)
        self.assertFalse(snapshot.tp_hit)


class TestPositionMonitorLifecycle(unittest.TestCase):
    def test_init(self) -> None:
        monitor = PositionMonitor()
        self.assertFalse(monitor._stop.is_set())
        self.assertEqual(monitor._cycle_count, 0)

    def test_stop(self) -> None:
        monitor = PositionMonitor()
        monitor.stop()
        self.assertTrue(monitor._stop.is_set())


if __name__ == "__main__":
    unittest.main()
