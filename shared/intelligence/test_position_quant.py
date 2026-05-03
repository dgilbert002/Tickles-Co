"""
Module: test_position_quant
Purpose: Unit tests for position_quant pure functions.
Location: /opt/tickles/shared/intelligence/test_position_quant.py
"""

import unittest
from datetime import datetime, timedelta, timezone

from shared.intelligence.position_quant import (
    check_sl_tp_hit,
    compute_distance_to_sl_tp,
    compute_max_adverse_excursion,
    compute_max_favorable_excursion,
    compute_pnl,
    compute_pnl_pct,
    compute_risk_reward_ratio,
    compute_time_metrics,
)


class TestComputePnl(unittest.TestCase):
    def test_long_profit(self) -> None:
        result = compute_pnl("long", 100.0, 110.0, 1.0)
        self.assertAlmostEqual(result, 10.0)

    def test_long_loss(self) -> None:
        result = compute_pnl("long", 100.0, 90.0, 1.0)
        self.assertAlmostEqual(result, -10.0)

    def test_short_profit(self) -> None:
        result = compute_pnl("short", 100.0, 90.0, 1.0)
        self.assertAlmostEqual(result, 10.0)

    def test_short_loss(self) -> None:
        result = compute_pnl("short", 100.0, 110.0, 1.0)
        self.assertAlmostEqual(result, -10.0)

    def test_leverage(self) -> None:
        result = compute_pnl("long", 100.0, 110.0, 1.0, leverage=10.0)
        self.assertAlmostEqual(result, 100.0)

    def test_invalid_side(self) -> None:
        with self.assertRaises(ValueError):
            compute_pnl("invalid", 100.0, 110.0, 1.0)

    def test_zero_price(self) -> None:
        with self.assertRaises(ValueError):
            compute_pnl("long", 0.0, 110.0, 1.0)


class TestComputePnlPct(unittest.TestCase):
    def test_long_up_5pct(self) -> None:
        result = compute_pnl_pct("long", 100.0, 105.0)
        self.assertAlmostEqual(result, 0.05)

    def test_short_up_5pct(self) -> None:
        result = compute_pnl_pct("short", 100.0, 105.0)
        self.assertAlmostEqual(result, -0.05)


class TestComputeDistanceToSlTp(unittest.TestCase):
    def test_long_with_sl_tp(self) -> None:
        result = compute_distance_to_sl_tp("long", 100.0, 95.0, 110.0)
        self.assertAlmostEqual(result["distance_to_sl"], 5.0)
        self.assertAlmostEqual(result["distance_to_tp"], 10.0)

    def test_no_sl_tp(self) -> None:
        result = compute_distance_to_sl_tp("long", 100.0, None, None)
        self.assertIsNone(result["distance_to_sl"])
        self.assertIsNone(result["distance_to_tp"])


class TestCheckSlTpHit(unittest.TestCase):
    def test_long_sl_hit(self) -> None:
        sl_hit, tp_hit = check_sl_tp_hit("long", 94.0, 95.0, 110.0)
        self.assertTrue(sl_hit)
        self.assertFalse(tp_hit)

    def test_long_tp_hit(self) -> None:
        sl_hit, tp_hit = check_sl_tp_hit("long", 111.0, 95.0, 110.0)
        self.assertFalse(sl_hit)
        self.assertTrue(tp_hit)

    def test_short_sl_hit(self) -> None:
        sl_hit, tp_hit = check_sl_tp_hit("short", 106.0, 105.0, 90.0)
        self.assertTrue(sl_hit)
        self.assertFalse(tp_hit)

    def test_short_tp_hit(self) -> None:
        sl_hit, tp_hit = check_sl_tp_hit("short", 89.0, 105.0, 90.0)
        self.assertFalse(sl_hit)
        self.assertTrue(tp_hit)

    def test_no_sl_tp(self) -> None:
        sl_hit, tp_hit = check_sl_tp_hit("long", 100.0, None, None)
        self.assertFalse(sl_hit)
        self.assertFalse(tp_hit)


class TestComputeRiskRewardRatio(unittest.TestCase):
    def test_valid(self) -> None:
        result = compute_risk_reward_ratio(100.0, 95.0, 110.0)
        self.assertAlmostEqual(result, 2.0)

    def test_missing_sl(self) -> None:
        result = compute_risk_reward_ratio(100.0, None, 110.0)
        self.assertIsNone(result)

    def test_zero_risk(self) -> None:
        result = compute_risk_reward_ratio(100.0, 100.0, 110.0)
        self.assertIsNone(result)


class TestComputeTimeMetrics(unittest.TestCase):
    def test_hours(self) -> None:
        entry = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        now = datetime(2026, 4, 26, 15, 30, 0, tzinfo=timezone.utc)
        result = compute_time_metrics(entry, now)
        self.assertAlmostEqual(result["hours_open"], 3.5)
        self.assertAlmostEqual(result["minutes_open"], 210.0)

    def test_default_now(self) -> None:
        entry = datetime.now(timezone.utc) - timedelta(hours=2)
        result = compute_time_metrics(entry)
        self.assertAlmostEqual(result["hours_open"], 2.0, places=1)


class TestComputeMaeMfe(unittest.TestCase):
    def test_long_mae(self) -> None:
        result = compute_max_adverse_excursion("long", 100.0, 97.0)
        self.assertAlmostEqual(result, 0.03)

    def test_long_mfe(self) -> None:
        result = compute_max_favorable_excursion("long", 100.0, 105.0)
        self.assertAlmostEqual(result, 0.05)

    def test_short_mae(self) -> None:
        result = compute_max_adverse_excursion("short", 100.0, 103.0)
        self.assertAlmostEqual(result, 0.03)

    def test_short_mfe(self) -> None:
        result = compute_max_favorable_excursion("short", 100.0, 95.0)
        self.assertAlmostEqual(result, 0.05)


if __name__ == "__main__":
    unittest.main()
