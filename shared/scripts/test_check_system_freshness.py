"""
Module: test_check_system_freshness
Purpose: Smoke tests for the system freshness audit utility.
Location: /opt/tickles/shared/scripts/test_check_system_freshness.py
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.append("/opt/tickles")

from shared.scripts.check_system_freshness import (
    _file_age_seconds,
    _parse_iso_ts,
    check_market_state,
    check_llm_state,
    check_scanner_output,
    run_audit,
)


class TestParseIsoTs(unittest.TestCase):
    def test_valid_iso(self) -> None:
        ts = "2026-04-24T10:00:00+00:00"
        result = _parse_iso_ts(ts)
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2026)

    def test_z_suffix(self) -> None:
        ts = "2026-04-24T10:00:00Z"
        result = _parse_iso_ts(ts)
        self.assertIsNotNone(result)

    def test_none(self) -> None:
        self.assertIsNone(_parse_iso_ts(None))

    def test_invalid(self) -> None:
        self.assertIsNone(_parse_iso_ts("not-a-date"))


class TestFileAgeSeconds(unittest.TestCase):
    def test_existing_file(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test")
            path = Path(f.name)
        try:
            age = _file_age_seconds(path)
            self.assertIsNotNone(age)
            self.assertGreaterEqual(age, 0)
        finally:
            os.unlink(path)

    def test_missing_file(self) -> None:
        age = _file_age_seconds(Path("/nonexistent/path"))
        self.assertIsNone(age)


class TestCheckMarketState(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fresh_market_state(self) -> None:
        data = {"timestamp": datetime.now(timezone.utc).isoformat(), "assets": {}}
        (self.tmpdir / "MARKET_STATE.json").write_text(json.dumps(data))
        (self.tmpdir / "MARKET_INDICATORS.json").write_text(json.dumps(data))

        fresh, msg = check_market_state(self.tmpdir, 300.0)
        self.assertTrue(fresh)
        self.assertIn("fresh", msg)

    def test_stale_market_state(self) -> None:
        old_ts = "2026-04-24T00:00:00+00:00"
        data = {"timestamp": old_ts, "assets": {}}
        (self.tmpdir / "MARKET_STATE.json").write_text(json.dumps(data))
        (self.tmpdir / "MARKET_INDICATORS.json").write_text(json.dumps(data))

        fresh, msg = check_market_state(self.tmpdir, 1.0)
        self.assertFalse(fresh)
        self.assertIn("STALE", msg)

    def test_missing_file(self) -> None:
        fresh, msg = check_market_state(self.tmpdir, 300.0)
        self.assertFalse(fresh)
        self.assertIn("MISSING", msg)


class TestCheckLlmState(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fresh_state(self) -> None:
        (self.tmpdir / ".surgeon_state.json").write_text("{}")

        fresh, msg = check_llm_state(self.tmpdir, 300.0)
        self.assertTrue(fresh)
        self.assertIn("fresh", msg)

    def test_missing_state(self) -> None:
        fresh, msg = check_llm_state(self.tmpdir, 300.0)
        self.assertFalse(fresh)
        self.assertIn("MISSING", msg)


class TestCheckScannerOutput(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fresh_scanner(self) -> None:
        data = {"timestamp": datetime.now(timezone.utc).isoformat(), "assets": {}}
        (self.tmpdir / "MARKET_STATE.json").write_text(json.dumps(data))
        (self.tmpdir / "MARKET_INDICATORS.json").write_text(json.dumps(data))

        fresh, msg = check_scanner_output(self.tmpdir, 300.0)
        self.assertTrue(fresh)
        self.assertIn("fresh", msg)


class TestRunAudit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        data = {"timestamp": datetime.now(timezone.utc).isoformat(), "assets": {}}
        (self.tmpdir / "MARKET_STATE.json").write_text(json.dumps(data))
        (self.tmpdir / "MARKET_INDICATORS.json").write_text(json.dumps(data))
        (self.tmpdir / ".surgeon_state.json").write_text("{}")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("shared.scripts.check_system_freshness._get_db_conn")
    def test_run_audit(self, mock_conn: MagicMock) -> None:
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            ("BTC/USDT", 0.0001, datetime.now(timezone.utc)),
            ("BTC/USDT", "1h", datetime.now(timezone.utc)),
        ]
        mock_conn.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = mock_cursor

        results = run_audit([self.tmpdir], [self.tmpdir], 300.0)
        self.assertIn("timestamp", results)
        self.assertIn("checks", results)


if __name__ == "__main__":
    unittest.main()
