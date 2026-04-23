"""
Smoke tests for shared.mcp.tools.data — M1 market data tools.

Tests the synchronous helper functions and handler logic without
requiring a running MCP daemon. Database-dependent tests are marked
with pytest.mark.integration and skipped unless DB is reachable.

Run:  python -m pytest shared/mcp/tools/test_data.py -v
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Unit tests for formatting / parsing helpers
# ---------------------------------------------------------------------------

from shared.mcp.tools.data import (
    _decimal_to_float,
    _fmt_ts,
    _format_candle,
    _format_coverage_row,
    _not_implemented,
    _parse_ts,
    _TF_MINUTES,
)


class TestNotImplemented:
    """Tests for _not_implemented helper."""

    def test_returns_status(self) -> None:
        result = _not_implemented("md.quote")
        assert result["status"] == "not_implemented"
        assert result["feature"] == "md.quote"
        assert "md.quote" in result["message"]


class TestFmtTs:
    """Tests for _fmt_ts timestamp formatter."""

    def test_none_returns_none(self) -> None:
        assert _fmt_ts(None) is None

    def test_datetime_to_iso(self) -> None:
        dt = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
        assert _fmt_ts(dt) == "2026-04-21T12:00:00+00:00"

    def test_string_passthrough(self) -> None:
        assert _fmt_ts("2026-04-21") == "2026-04-21"


class TestParseTs:
    """Tests for _parse_ts timestamp parser."""

    def test_iso_with_z(self) -> None:
        dt = _parse_ts("2026-04-21T08:00:00Z")
        assert dt.year == 2026
        assert dt.tzinfo is not None

    def test_iso_with_offset(self) -> None:
        dt = _parse_ts("2026-04-21T08:00:00+00:00")
        assert dt.hour == 8

    def test_naive_gets_utc(self) -> None:
        dt = _parse_ts("2026-04-21T08:00:00")
        assert dt.tzinfo is not None

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid timestamp"):
            _parse_ts("not-a-date")


class TestDecimalToFloat:
    """Tests for _decimal_to_float."""

    def test_none(self) -> None:
        assert _decimal_to_float(None) is None

    def test_int(self) -> None:
        assert _decimal_to_float(42) == 42.0

    def test_float(self) -> None:
        assert _decimal_to_float(3.14) == 3.14


class TestFormatCandle:
    """Tests for _format_candle row formatter."""

    def test_basic_row(self) -> None:
        row = {
            "timestamp": datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
            "open": 76000.5,
            "high": 76100.0,
            "low": 75900.0,
            "close": 76050.0,
            "volume": 123.456,
            "source": "bybit",
        }
        result = _format_candle(row)
        assert result["timestamp"] == "2026-04-21T12:00:00+00:00"
        assert result["open"] == 76000.5
        assert result["source"] == "bybit"

    def test_null_volume(self) -> None:
        row = {
            "timestamp": datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": None,
            "source": "binance",
        }
        result = _format_candle(row)
        assert result["volume"] is None


class TestFormatCoverageRow:
    """Tests for _format_coverage_row."""

    def test_fresh_data(self) -> None:
        now = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
        row = {
            "symbol": "BTC/USDT",
            "exchange": "bybit",
            "source": "bybit",
            "timeframe": "1m",
            "bars": 1000,
            "first_ts": datetime(2026, 4, 20, 0, 0, tzinfo=timezone.utc),
            "last_ts": datetime(2026, 4, 21, 11, 59, tzinfo=timezone.utc),
        }
        result = _format_coverage_row(row, now)
        assert result["symbol"] == "BTC/USDT"
        assert result["venue"] == "bybit"
        assert result["bars"] == 1000
        assert result["fresh_lag_minutes"] == 1

    def test_no_data(self) -> None:
        now = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
        row = {
            "symbol": "ETH/USDT",
            "exchange": "binance",
            "source": "binance",
            "timeframe": "1h",
            "bars": 0,
            "first_ts": None,
            "last_ts": None,
        }
        result = _format_coverage_row(row, now)
        assert result["fresh_lag_minutes"] is None
        assert result["first_ts"] is None


class TestTfMinutes:
    """Tests for _TF_MINUTES mapping."""

    def test_all_timeframes_present(self) -> None:
        for tf in ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"):
            assert tf in _TF_MINUTES

    def test_values(self) -> None:
        assert _TF_MINUTES["1m"] == 1
        assert _TF_MINUTES["1h"] == 60
        assert _TF_MINUTES["1d"] == 1440


# ---------------------------------------------------------------------------
# Handler unit tests (mocked DB)
# ---------------------------------------------------------------------------

class TestMdQuoteHandler:
    """Tests for _handle_md_quote with mocked db_helper."""

    def test_instrument_not_found(self) -> None:
        from shared.mcp.tools.data import _handle_md_quote
        with patch("shared.mcp.tools.data.resolve_instrument_id", return_value=None):
            result = _handle_md_quote({"symbol": "FAKE/USDT", "venue": "bybit"})
            assert result["status"] == "error"
            assert "not found" in result["message"].lower()

    def test_no_candles(self) -> None:
        from shared.mcp.tools.data import _handle_md_quote
        with patch("shared.mcp.tools.data.resolve_instrument_id", return_value=1), \
             patch("shared.mcp.tools.data.query", return_value=[]):
            result = _handle_md_quote({"symbol": "BTC/USDT", "venue": "bybit"})
            assert result["status"] == "no_data"

    def test_success(self) -> None:
        from shared.mcp.tools.data import _handle_md_quote
        with patch("shared.mcp.tools.data.resolve_instrument_id", return_value=1), \
             patch("shared.mcp.tools.data.query", return_value=[{
                 "timestamp": datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
                 "open": 76000.0, "high": 76100.0, "low": 75900.0,
                 "close": 76050.0, "volume": 100.0, "source": "bybit",
             }]):
            result = _handle_md_quote({"symbol": "BTC/USDT", "venue": "bybit"})
            assert result["status"] == "ok"
            assert result["close"] == 76050.0


class TestMdCandlesHandler:
    """Tests for _handle_md_candles with mocked db_helper."""

    def test_instrument_not_found(self) -> None:
        from shared.mcp.tools.data import _handle_md_candles
        with patch("shared.mcp.tools.data.resolve_instrument_id", return_value=None):
            result = _handle_md_candles({"symbol": "FAKE/USDT"})
            assert result["status"] == "error"

    def test_success_with_candles(self) -> None:
        from shared.mcp.tools.data import _handle_md_candles
        with patch("shared.mcp.tools.data.resolve_instrument_id", return_value=1), \
             patch("shared.mcp.tools.data.query", return_value=[{
                 "timestamp": datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
                 "open": 76000.0, "high": 76100.0, "low": 75900.0,
                 "close": 76050.0, "volume": 100.0, "source": "bybit",
             }]):
            result = _handle_md_candles({"symbol": "BTC/USDT", "timeframe": "1m", "limit": 10})
            assert result["status"] == "ok"
            assert result["count"] == 1
            assert len(result["candles"]) == 1


class TestCandlesCoverageHandler:
    """Tests for _handle_candles_coverage with mocked db_helper."""

    def test_symbol_without_venue(self) -> None:
        from shared.mcp.tools.data import _handle_candles_coverage
        result = _handle_candles_coverage({"symbol": "BTC/USDT"})
        assert result["status"] == "error"
        assert "both" in result["message"].lower()

    def test_success(self) -> None:
        from shared.mcp.tools.data import _handle_candles_coverage
        with patch("shared.mcp.tools.data.query", return_value=[{
            "symbol": "BTC/USDT",
            "exchange": "bybit",
            "source": "bybit",
            "timeframe": "1m",
            "bars": 5000,
            "first_ts": datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
            "last_ts": datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        }]):
            result = _handle_candles_coverage({})
            assert result["status"] == "ok"
            assert result["count"] == 1
