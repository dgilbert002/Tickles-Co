"""
Module: test_market_routes_fallback
Purpose: Unit tests for the write-through CCXT fallback in dashboard market routes.
Location: /opt/tickles/shared/tests/test_market_routes_fallback.py
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock
from decimal import Decimal

from shared.dashboard.market_routes import _fetch_native_candles
from shared.connectors.base import Candle

@pytest.mark.asyncio
async def test_fetch_native_candles_db_hit() -> None:
    """Verify that if DB returns candles, they are returned directly without calling CCXT fallback."""
    fake_rows = [
        {
            "timestamp": datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
            "open": Decimal("100.0"),
            "high": Decimal("105.0"),
            "low": Decimal("95.0"),
            "close": Decimal("101.0"),
            "volume": Decimal("1000.0"),
            "is_fake": False,
            "symbol": "BTC/USDT",
            "exchange": "bybit",
        }
    ]
    
    mock_pool = MagicMock()
    mock_pool.fetch_all = AsyncMock(return_value=fake_rows)
    
    with patch("shared.dashboard.market_routes.get_shared_pool", return_value=mock_pool), \
         patch("shared.connectors.ccxt_adapter.CCXTAdapter") as mock_ccxt_adapter:
         
        res = await _fetch_native_candles(
            symbol="BTC/USDT",
            exchange="bybit",
            timeframe="1d",
            start=datetime(2026, 5, 22, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 5, 22, 23, 59, tzinfo=timezone.utc),
            limit=10,
        )
        
        assert len(res) == 1
        assert res[0]["close"] == Decimal("101.0")
        mock_ccxt_adapter.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_native_candles_fallback_trigger() -> None:
    """Verify that if DB returns empty, it triggers CCXT fallback and inserts candles into the DB."""
    mock_pool = MagicMock()
    # First query for target_instruments -> rows is empty -> triggers fallback
    # Fallback query for instrument -> returns inst
    # After insertion, we query DB again -> returns fallback row
    
    inst_row = {
        "id": 42,
        "symbol": "BTC/USDT",
        "exchange": "bybit",
    }
    
    fallback_candles = [
        {
            "timestamp": datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
            "open": Decimal("100.0"),
            "high": Decimal("105.0"),
            "low": Decimal("95.0"),
            "close": Decimal("101.0"),
            "volume": Decimal("1000.0"),
            "is_fake": False,
            "symbol": "BTC/USDT",
            "exchange": "bybit",
        }
    ]
    
    # Mocking different queries
    # Query 1: rows = []
    # Query 2: inst_row = inst_row
    # Query 3: insert candle (conn.execute)
    # Query 4: rows after insertion = fallback_candles
    mock_pool.fetch_all = AsyncMock(side_effect=[[], fallback_candles])
    mock_pool.fetch_one = AsyncMock(return_value=inst_row)
    
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()
    
    # Mock context manager for acquire()
    mock_acquire_cm = MagicMock()
    mock_acquire_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_acquire_cm.__aexit__ = AsyncMock(return_value=None)
    mock_pool.acquire = MagicMock(return_value=mock_acquire_cm)
    
    # Mock CCXTAdapter
    mock_adapter_instance = MagicMock()
    mock_adapter_instance.fetch_ohlcv = AsyncMock(return_value=[
        Candle(
            instrument_id="BTC/USDT",
            timeframe="1d",
            timestamp=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
            open=Decimal("100.0"),
            high=Decimal("105.0"),
            low=Decimal("95.0"),
            close=Decimal("101.0"),
            volume=Decimal("1000.0"),
            quote_volume=Decimal("101000.0"),
            trades_count=None,
            data_source="api",
            candle_data_hash="fakehash",
            exchange="bybit",
        )
    ])
    mock_adapter_instance.close = AsyncMock()
    
    with patch("shared.dashboard.market_routes.get_shared_pool", return_value=mock_pool), \
         patch("shared.connectors.ccxt_adapter.CCXTAdapter", return_value=mock_adapter_instance):
         
        res = await _fetch_native_candles(
            symbol="BTC/USDT",
            exchange="bybit",
            timeframe="1d",
            start=datetime(2026, 5, 22, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 5, 22, 23, 59, tzinfo=timezone.utc),
            limit=10,
        )
        
        # Verify candles were fetched and written to DB
        assert len(res) == 1
        assert res[0]["close"] == Decimal("101.0")
        
        # Verify CCXT Adapter was called with normalized arguments
        mock_adapter_instance.fetch_ohlcv.assert_called_once()
        mock_conn.execute.assert_called_once()
        mock_adapter_instance.close.assert_called_once()
