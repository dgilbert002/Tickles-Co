"""
Module: test_signal_to_position_wiring
Purpose: Smoke test for signal_interpretations → tracked_positions wiring.
Location: /opt/tickles/shared/tests/test_signal_to_position_wiring.py
"""

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from shared.intelligence.interpretation_service import (
    ConsensusResult,
    LlmResult,
    QuantResult,
    create_tracked_position_from_interpretation,
    write_signal_interpretation,
)


class TestWriteSignalInterpretationReturnsId(unittest.TestCase):
    def test_returns_id_on_insert(self) -> None:
        """write_signal_interpretation must return the inserted row ID."""
        loop = asyncio.new_event_loop()
        try:
            pool = MagicMock()
            pool.fetch_one = AsyncMock(return_value={"id": 42})

            consensus = ConsensusResult(
                direction="long",
                confidence=0.85,
                method="llm+quant",
                llm_result=LlmResult(
                    direction="long",
                    confidence=0.9,
                    reasoning="Bullish breakout",
                    levels={"entry": "65000", "stop_loss": "64000", "take_profit": "70000"},
                    model_used="test-model",
                    cost_usd=0.001,
                ),
                quant_result=QuantResult(
                    direction="long",
                    confidence=0.7,
                    indicators={"rsi": 55.0},
                    cost_usd=0.0,
                ),
            )

            result = loop.run_until_complete(
                write_signal_interpretation(
                    shared_pool=pool,
                    news_item_id=1,
                    media_item_id=2,
                    trader_profile_id=3,
                    consensus=consensus,
                    param_hash="abc123",
                    candle_data_hash="def456",
                    instrument_symbol="BTCUSDT",
                    exchange="bybit",
                    market_data_fresh=True,
                    market_data_at=datetime.now(timezone.utc),
                )
            )
            self.assertEqual(result, 42)
            pool.fetch_one.assert_called_once()
        finally:
            loop.close()

    def test_returns_none_on_conflict(self) -> None:
        """write_signal_interpretation must return None on dedup conflict."""
        loop = asyncio.new_event_loop()
        try:
            pool = MagicMock()
            pool.fetch_one = AsyncMock(return_value=None)

            consensus = ConsensusResult(
                direction="long",
                confidence=0.85,
                method="llm+quant",
                llm_result=None,
                quant_result=None,
            )

            result = loop.run_until_complete(
                write_signal_interpretation(
                    shared_pool=pool,
                    news_item_id=1,
                    media_item_id=2,
                    trader_profile_id=3,
                    consensus=consensus,
                    param_hash="abc123",
                    candle_data_hash="def456",
                    instrument_symbol="BTCUSDT",
                    exchange="bybit",
                    market_data_fresh=True,
                    market_data_at=datetime.now(timezone.utc),
                )
            )
            self.assertIsNone(result)
        finally:
            loop.close()


class TestCreateTrackedPositionFromInterpretation(unittest.TestCase):
    def test_skips_non_tradeable_direction(self) -> None:
        """Must skip neutral/unclear directions."""
        loop = asyncio.new_event_loop()
        try:
            pool = MagicMock()
            pool.fetch_one = AsyncMock(return_value={"id": 99})

            result = loop.run_until_complete(
                create_tracked_position_from_interpretation(
                    shared_pool=pool,
                    signal_interpretation_id=1,
                    news_item_id=2,
                    media_item_id=3,
                    trader_profile_id=4,
                    instrument_symbol="BTCUSDT",
                    instrument_exchange="bybit",
                    direction="neutral",
                    entry_price=65000.0,
                    stop_loss=64000.0,
                    take_profit_1=70000.0,
                    detection_method="llm+quant",
                    detection_confidence=0.85,
                    raw_signal_text="Bullish breakout",
                )
            )
            self.assertIsNone(result)
            pool.fetch_one.assert_not_called()
        finally:
            loop.close()

    def test_creates_position_for_long(self) -> None:
        """Must create tracked_position for long direction."""
        loop = asyncio.new_event_loop()
        try:
            pool = MagicMock()
            pool.fetch_one = AsyncMock(return_value={"id": 100})

            result = loop.run_until_complete(
                create_tracked_position_from_interpretation(
                    shared_pool=pool,
                    signal_interpretation_id=1,
                    news_item_id=2,
                    media_item_id=3,
                    trader_profile_id=4,
                    instrument_symbol="BTCUSDT",
                    instrument_exchange="bybit",
                    direction="long",
                    entry_price=65000.0,
                    stop_loss=64000.0,
                    take_profit_1=70000.0,
                    detection_method="llm+quant",
                    detection_confidence=0.85,
                    raw_signal_text="Bullish breakout",
                )
            )
            self.assertEqual(result, 100)
            pool.fetch_one.assert_called_once()
            call_args = pool.fetch_one.call_args
            self.assertIn("tracked_positions", call_args[0][0])
        finally:
            loop.close()

    def test_returns_none_on_conflict(self) -> None:
        """Must return None when dedup conflict hits."""
        loop = asyncio.new_event_loop()
        try:
            pool = MagicMock()
            pool.fetch_one = AsyncMock(return_value=None)

            result = loop.run_until_complete(
                create_tracked_position_from_interpretation(
                    shared_pool=pool,
                    signal_interpretation_id=1,
                    news_item_id=2,
                    media_item_id=3,
                    trader_profile_id=4,
                    instrument_symbol="BTCUSDT",
                    instrument_exchange="bybit",
                    direction="short",
                    entry_price=65000.0,
                    stop_loss=66000.0,
                    take_profit_1=60000.0,
                    detection_method="llm+quant",
                    detection_confidence=0.85,
                    raw_signal_text="Bearish breakdown",
                )
            )
            self.assertIsNone(result)
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
