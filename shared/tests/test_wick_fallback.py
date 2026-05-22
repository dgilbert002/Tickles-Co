"""
Module: test_wick_fallback
Purpose: Test context window formatting and wick/touch fallback logic in position_monitor.
Location: /opt/tickles/shared/tests/test_wick_fallback.py
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.intelligence.interpretation_service import _format_context_window
from shared.intelligence.position_monitor import (
    _find_entry_touch_candle,
    _find_sl_tp_wick_candle,
)


def test_format_context_window_basic() -> None:
    """Verify context window formats messages from the same author only."""
    context_window = {
        "before": [
            {
                "author": "TraderA",
                "text": "Buying zone established.",
                "timestamp": "2026-05-22T10:00:00Z",
            },
            {
                "author": "TraderB",
                "text": "I agree with this setup.",
                "timestamp": "2026-05-22T10:05:00Z",
            },
        ],
        "after": [
            {
                "author": "TraderA",
                "text": "Moving target slightly higher.",
                "timestamp": "2026-05-22T10:15:00Z",
            }
        ],
    }

    # TraderA's messages should be included, TraderB's should be excluded
    formatted = _format_context_window(context_window, "TraderA")
    assert "Buying zone established" in formatted
    assert "Moving target slightly higher" in formatted
    assert "I agree with this setup" not in formatted
    assert "TraderA:" in formatted
    assert "TraderB:" not in formatted


def test_format_context_window_json_string() -> None:
    """Verify context window parsing also handles JSON string input."""
    context_window_str = json.dumps({
        "before": [
            {
                "author": "TraderA",
                "text": "Checking chart.",
                "timestamp": "2026-05-22T10:00:00Z",
            }
        ]
    })
    formatted = _format_context_window(context_window_str, "TraderA")
    assert "Checking chart." in formatted


def test_format_context_window_empty_or_invalid() -> None:
    """Verify edge cases for invalid inputs return empty string gracefully."""
    assert _format_context_window(None, "TraderA") == ""
    assert _format_context_window("", "TraderA") == ""
    assert _format_context_window("invalid json", "TraderA") == ""
    assert _format_context_window({}, "TraderA") == ""


class FakeCandle:
    def __init__(self, timestamp: datetime, high: float, low: float, close: float) -> None:
        self.timestamp = timestamp
        self.high = high
        self.low = low
        self.close = close


class FakePool:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []

    async def fetch_all(self, sql: str, *args: any) -> list[dict]:
        return self.rows

    async def fetch_one(self, sql: str, *args: any) -> dict | None:
        return self.rows[0] if self.rows else None


@pytest.mark.asyncio
async def test_find_sl_tp_wick_candle_fallback() -> None:
    """Verify that when local candles are empty, we fall back to CCXT fetch_ohlcv."""
    pool = FakePool(rows=[])
    since = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)

    fake_candles = [
        FakeCandle(
            timestamp=datetime(2026, 5, 22, 10, 5, 0, tzinfo=timezone.utc),
            high=61500.0,
            low=59800.0,
            close=60500.0,
        )
    ]

    mock_adapter = MagicMock()
    mock_adapter.fetch_ohlcv = AsyncMock(return_value=fake_candles)
    mock_adapter.close = AsyncMock()

    with patch("shared.connectors.ccxt_adapter.CCXTAdapter", return_value=mock_adapter), \
         patch("shared.intelligence.position_monitor._resolve_instrument_id", return_value=42):

        # Long trade: entry=60000. SL=59900 (should be triggered by low=59800.0)
        res = await _find_sl_tp_wick_candle(
            pool=pool,
            symbol="BTC/USDT",
            exchange="bybit",
            timeframe="1m",
            direction="long",
            stop_loss=59900.0,
            take_profit=61000.0,
            since=since,
        )

        assert res is not None
        ts, close, trigger, hit_val = res
        assert trigger == "sl"
        assert hit_val == 59900.0
        assert close == 60500.0
        mock_adapter.fetch_ohlcv.assert_called_once()
        mock_adapter.close.assert_called_once()


@pytest.mark.asyncio
async def test_find_entry_touch_candle_fallback() -> None:
    """Verify that when local touch candle is missing, we fall back to CCXT fetch_ohlcv."""
    pool = FakePool(rows=[])
    since = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)

    fake_candles = [
        FakeCandle(
            timestamp=datetime(2026, 5, 22, 10, 5, 0, tzinfo=timezone.utc),
            high=60500.0,
            low=59500.0,
            close=60100.0,
        )
    ]

    mock_adapter = MagicMock()
    mock_adapter.fetch_ohlcv = AsyncMock(return_value=fake_candles)
    mock_adapter.close = AsyncMock()

    with patch("shared.connectors.ccxt_adapter.CCXTAdapter", return_value=mock_adapter), \
         patch("shared.intelligence.position_monitor._resolve_instrument_id", return_value=42):

        # Entry = 60000.0 (touches within [59500, 60500])
        res = await _find_entry_touch_candle(
            pool=pool,
            symbol="BTC/USDT",
            exchange="bybit",
            timeframe="1m",
            entry=60000.0,
            since=since,
        )

        assert res is not None
        ts, close = res
        assert ts == datetime(2026, 5, 22, 10, 5, 0, tzinfo=timezone.utc)
        assert close == 60100.0
        mock_adapter.fetch_ohlcv.assert_called_once()
        mock_adapter.close.assert_called_once()


@pytest.mark.asyncio
async def test_find_sl_tp_wick_candle_cached_adapters() -> None:
    """Verify that cached adapters are preserved and NOT closed inside the fallback."""
    pool = FakePool(rows=[])
    since = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)
    adapters_cache = {}

    fake_candles = [
        FakeCandle(
            timestamp=datetime(2026, 5, 22, 10, 5, 0, tzinfo=timezone.utc),
            high=61500.0,
            low=59800.0,
            close=60500.0,
        )
    ]

    mock_adapter = MagicMock()
    mock_adapter.fetch_ohlcv = AsyncMock(return_value=fake_candles)
    mock_adapter.close = AsyncMock()

    with patch("shared.connectors.ccxt_adapter.CCXTAdapter", return_value=mock_adapter), \
         patch("shared.intelligence.position_monitor._resolve_instrument_id", return_value=42):

        res = await _find_sl_tp_wick_candle(
            pool=pool,
            symbol="BTC/USDT",
            exchange="bybit",
            timeframe="1m",
            direction="long",
            stop_loss=59900.0,
            take_profit=61000.0,
            since=since,
            adapters=adapters_cache,
        )

        assert res is not None
        # Should be stored in adapters cache
        assert "bybit" in adapters_cache
        assert adapters_cache["bybit"] == mock_adapter
        # fetch_ohlcv should be called, but adapter.close() should NOT be called since it is cached!
        mock_adapter.fetch_ohlcv.assert_called_once()
        mock_adapter.close.assert_not_called()


@pytest.mark.asyncio
async def test_timezone_naive_guard() -> None:
    """Verify that timezone naive inputs are handled gracefully and force-converted to UTC."""
    pool = FakePool(rows=[])
    # Naive timestamp (no tzinfo)
    since_naive = datetime(2026, 5, 22, 10, 0, 0)

    fake_candles = [
        FakeCandle(
            timestamp=datetime(2026, 5, 22, 10, 5, 0, tzinfo=timezone.utc),
            high=60500.0,
            low=59500.0,
            close=60100.0,
        )
    ]

    mock_adapter = MagicMock()
    mock_adapter.fetch_ohlcv = AsyncMock(return_value=fake_candles)
    mock_adapter.close = AsyncMock()

    with patch("shared.connectors.ccxt_adapter.CCXTAdapter", return_value=mock_adapter), \
         patch("shared.intelligence.position_monitor._resolve_instrument_id", return_value=42):

        res = await _find_entry_touch_candle(
            pool=pool,
            symbol="BTC/USDT",
            exchange="bybit",
            timeframe="1m",
            entry=60000.0,
            since=since_naive,
        )

        assert res is not None
        mock_adapter.fetch_ohlcv.assert_called_once()
        # Verify that the since argument passed to fetch_ohlcv was converted to UTC
        called_args, called_kwargs = mock_adapter.fetch_ohlcv.call_args
        called_since = called_kwargs.get("since") or called_args[2] # since is either kwargs or positional
        assert called_since.tzinfo is not None
        assert called_since.tzinfo == timezone.utc


@pytest.mark.asyncio
async def test_resilient_symbol_probing() -> None:
    """Verify that if the first symbol query fails, we try the candidate symbol loop fallback."""
    pool = FakePool(rows=[])
    since = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)

    fake_candles = [
        FakeCandle(
            timestamp=datetime(2026, 5, 22, 10, 5, 0, tzinfo=timezone.utc),
            high=61500.0,
            low=59800.0,
            close=60500.0,
        )
    ]

    mock_adapter = MagicMock()
    # First call with raw BTCUSDT raises error, second call with BTC/USDT succeeds!
    mock_adapter.fetch_ohlcv = AsyncMock(side_effect=[Exception("BadSymbol"), fake_candles])
    mock_adapter.close = AsyncMock()

    with patch("shared.connectors.ccxt_adapter.CCXTAdapter", return_value=mock_adapter), \
         patch("shared.intelligence.position_monitor._resolve_instrument_id", return_value=42):

        # We pass raw "BTCUSDT" (which _candidate_symbols resolves to ['BTCUSDT', 'BTC/USDT'])
        res = await _find_sl_tp_wick_candle(
            pool=pool,
            symbol="BTCUSDT",
            exchange="bybit",
            timeframe="1m",
            direction="long",
            stop_loss=59900.0,
            take_profit=61000.0,
            since=since,
        )

        assert res is not None
        # fetch_ohlcv should have been called twice (probing fallbacks)
        assert mock_adapter.fetch_ohlcv.call_count == 2
        mock_adapter.close.assert_called_once()

