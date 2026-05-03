"""
Module: test_freshness
Purpose: Unit tests for Freshness Guard utility.
Location: /opt/tickles/shared/utils/test_freshness.py
"""

import unittest
from datetime import datetime, timedelta, timezone
from shared.utils.freshness import validate_freshness, freshness_envelope, StaleDataError

class TestFreshnessGuard(unittest.TestCase):
    def test_validate_freshness_ok(self):
        # Recent timestamp
        ts = datetime.now(timezone.utc) - timedelta(seconds=10)
        lag = validate_freshness(ts, threshold_seconds=30)
        self.assertLess(lag, 30)
        self.assertGreater(lag, 0)

    def test_validate_freshness_stale(self):
        # Old timestamp
        ts = datetime.now(timezone.utc) - timedelta(seconds=200)
        with self.assertRaises(StaleDataError):
            validate_freshness(ts, threshold_seconds=180)

    def test_validate_freshness_iso_string(self):
        ts = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        lag = validate_freshness(ts, threshold_seconds=30)
        self.assertLess(lag, 30)

    def test_freshness_envelope_ok(self):
        data = {"timestamp": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(), "value": 42}
        res = freshness_envelope(data, threshold_seconds=30)
        self.assertEqual(res["freshness"]["status"], "fresh")
        self.assertEqual(res["value"], 42)

    def test_freshness_envelope_stale(self):
        data = {"timestamp": (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat(), "value": 42}
        res = freshness_envelope(data, threshold_seconds=180)
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["error_code"], "STALE_DATA")
        self.assertIn("lag_seconds", res)

    def test_freshness_envelope_nested(self):
        data = {
            "ticker": {
                "timestamp": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
            },
            "symbol": "BTC/USDT"
        }
        res = freshness_envelope(data, ts_field="ticker.timestamp", threshold_seconds=30)
        self.assertEqual(res["freshness"]["status"], "fresh")
        self.assertEqual(res["symbol"], "BTC/USDT")

    def test_freshness_envelope_nested_missing(self):
        data = {"ticker": {}, "symbol": "BTC/USDT"}
        res = freshness_envelope(data, ts_field="ticker.timestamp", threshold_seconds=30)
        self.assertEqual(res["status"], "error")
        self.assertIn("null timestamp", res["message"])

if __name__ == "__main__":
    unittest.main()
