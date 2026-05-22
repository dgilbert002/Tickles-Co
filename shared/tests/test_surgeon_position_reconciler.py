"""
Module: test_surgeon_position_reconciler
Purpose: Unit tests for the Surgeon position reconciler helper functions and
         lookback-window filtering. Network/DB-touching paths are not exercised
         here; integration coverage is handled by the broader pipeline tests.
Location: /opt/tickles/shared/tests/test_surgeon_position_reconciler.py
"""
from __future__ import annotations

import logging
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from shared.intelligence.surgeon_position_reconciler import (
    _coerce_ts,
    _load_surgeon1_state,
    _resolve_companies,
    _safe_float,
    _v1_open_positions,
    _v1_recent_closes,
    parse_duration,
)

logger = logging.getLogger(__name__)


class TestParseDuration(unittest.TestCase):
    """Verify parse_duration covers the supported unit grammar."""

    def test_supports_all_units(self) -> None:
        """parse_duration must accept s/m/h/d/w."""
        try:
            self.assertEqual(parse_duration("30s"), timedelta(seconds=30))
            self.assertEqual(parse_duration("15m"), timedelta(minutes=15))
            self.assertEqual(parse_duration("3h"), timedelta(hours=3))
            self.assertEqual(parse_duration("7d"), timedelta(days=7))
            self.assertEqual(parse_duration("2w"), timedelta(weeks=2))
        except Exception as exc:
            logger.exception("parse_duration test failed: %s", exc)
            raise

    def test_rejects_garbage(self) -> None:
        """Bad inputs must raise ValueError."""
        for bad in ("garbage", "", "1y", "10", None, 5):
            try:
                with self.assertRaises(ValueError):
                    parse_duration(bad)  # type: ignore[arg-type]
            except Exception as exc:
                logger.exception("parse_duration rejection failed for %r: %s", bad, exc)
                raise


class TestCoerceTs(unittest.TestCase):
    """Verify _coerce_ts robustly normalises timestamps."""

    def test_handles_iso_naive_and_aware(self) -> None:
        """ISO strings, datetimes, and epochs all become UTC-aware datetimes."""
        try:
            naive = datetime(2026, 5, 4, 12, 0, 0)
            self.assertEqual(_coerce_ts(naive).tzinfo, timezone.utc)
            iso = "2026-05-04T12:00:00Z"
            self.assertIsNotNone(_coerce_ts(iso))
            self.assertIsNotNone(_coerce_ts(1_700_000_000))
            self.assertIsNone(_coerce_ts(None))
            self.assertIsNone(_coerce_ts("not-a-date"))
        except Exception as exc:
            logger.exception("_coerce_ts test failed: %s", exc)
            raise


class TestSafeFloat(unittest.TestCase):
    """Verify _safe_float never raises."""

    def test_handles_various_inputs(self) -> None:
        """Returns floats for numeric input, None for everything else."""
        try:
            self.assertEqual(_safe_float("1.5"), 1.5)
            self.assertEqual(_safe_float(2), 2.0)
            self.assertIsNone(_safe_float(None))
            self.assertIsNone(_safe_float("abc"))
            self.assertIsNone(_safe_float([]))
        except Exception as exc:
            logger.exception("_safe_float test failed: %s", exc)
            raise


class TestV1OpenPositions(unittest.TestCase):
    """Verify v1 open-position iterator filters correctly."""

    def test_filters_remaining_frac(self) -> None:
        """Only positions with remaining_frac > 0 are yielded."""
        try:
            state = {
                "positions": [
                    {"symbol": "BTCUSDT", "remaining_frac": 1.0},
                    {"symbol": "ETHUSDT", "remaining_frac": 0.0},
                    {"symbol": "XRPUSDT", "remaining_frac": None},
                    "not-a-dict",
                ]
            }
            opens = list(_v1_open_positions(state))
            self.assertEqual(len(opens), 1)
            self.assertEqual(opens[0]["symbol"], "BTCUSDT")
        except Exception as exc:
            logger.exception("_v1_open_positions test failed: %s", exc)
            raise


class TestV1RecentCloses(unittest.TestCase):
    """Verify v1 close iterator filters by ts cutoff and remaining_after."""

    def test_filters_by_window_and_remaining(self) -> None:
        """Only fully-closed fills inside the lookback window survive."""
        try:
            now = datetime.now(timezone.utc)
            state = {
                "closed_trades": [
                    {"symbol": "BTC", "ts": now.isoformat(), "remaining_after": 0.0},
                    {
                        "symbol": "ETH",
                        "ts": (now - timedelta(days=30)).isoformat(),
                        "remaining_after": 0.0,
                    },
                    {"symbol": "XRP", "ts": now.isoformat(), "remaining_after": 0.5},
                    "bad",
                ]
            }
            closes = list(_v1_recent_closes(state, now - timedelta(days=7)))
            self.assertEqual(len(closes), 1)
            self.assertEqual(closes[0][1]["symbol"], "BTC")
        except Exception as exc:
            logger.exception("_v1_recent_closes test failed: %s", exc)
            raise


class TestLoadSurgeon1State(unittest.TestCase):
    """Verify the v1 state loader handles missing/invalid files gracefully."""

    def test_missing_file_returns_none(self) -> None:
        """Non-existent path returns None."""
        try:
            self.assertIsNone(_load_surgeon1_state(Path("/nonexistent/path/x.json")))
        except Exception as exc:
            logger.exception("missing-file test failed: %s", exc)
            raise

    def test_invalid_json_returns_none(self) -> None:
        """Malformed JSON returns None instead of raising."""
        try:
            with TemporaryDirectory() as tmp:
                p = Path(tmp) / "broken.json"
                p.write_text("{not-json")
                self.assertIsNone(_load_surgeon1_state(p))
        except Exception as exc:
            logger.exception("invalid-json test failed: %s", exc)
            raise

    def test_valid_json_returns_dict(self) -> None:
        """Good JSON is parsed into a dict."""
        try:
            with TemporaryDirectory() as tmp:
                p = Path(tmp) / "ok.json"
                p.write_text('{"positions": [], "closed_trades": []}')
                state = _load_surgeon1_state(p)
                self.assertIsInstance(state, dict)
                self.assertIn("positions", state)
        except Exception as exc:
            logger.exception("valid-json test failed: %s", exc)
            raise


class TestResolveCompanies(unittest.TestCase):
    """Verify CLI/env company-list resolution."""

    def test_explicit_arg_wins(self) -> None:
        """An explicit --company list overrides everything else."""
        try:
            self.assertEqual(_resolve_companies(["a", "b"]), ["a", "b"])
        except Exception as exc:
            logger.exception("explicit-arg test failed: %s", exc)
            raise

    def test_default_is_rubicon(self) -> None:
        """With no arg and no env, default resolves to rubicon."""
        try:
            import os
            os.environ.pop("SURGEON_RECON_COMPANIES", None)
            self.assertEqual(_resolve_companies(None), ["rubicon"])
        except Exception as exc:
            logger.exception("default-rubicon test failed: %s", exc)
            raise


if __name__ == "__main__":
    unittest.main()
