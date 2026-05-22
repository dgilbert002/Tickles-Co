"""
Module: test_f5_entry_price_guard
Purpose: Verify F5 sanity guard refuses tracked_position insert when
         entry_price IS NULL (or non-positive), with env-var override
         for the F9 backfill path.
Location: /opt/tickles/shared/tests/test_f5_entry_price_guard.py

This is the gate that prevents fresh orphan positions from being created
during normal interpretation flow. The 77 existing orphans (PHASE_X0
diagnosis) were inserted before this guard existed; F9's backfill script
will resolve them retroactively while running with ALLOW_NULL_ENTRY_PRICE=1.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

from shared.intelligence.interpretation_service import (
    create_tracked_position_from_interpretation,
)


# ---------------------------------------------------------------------------
# Fake pool — captures fetch_one calls without touching Postgres
# ---------------------------------------------------------------------------


class _FakePool:
    """In-memory stand-in for shared.utils.db.DatabasePool.

    Records every fetch_one call so the test can assert whether an INSERT
    was attempted. Returns a fixed RETURNING id when allowed.
    """

    def __init__(self, returning_id: Optional[int] = 12345) -> None:
        self.calls: List[Tuple[str, Any]] = []
        self.returning_id = returning_id

    async def fetch_one(self, sql: str, params: Any = None) -> Optional[Dict[str, Any]]:
        self.calls.append((sql, params))
        if "tracked_positions" in sql and self.returning_id is not None:
            return {"id": self.returning_id}
        return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_entry_price_is_rejected(caplog: pytest.LogCaptureFixture) -> None:
    """entry_price=None must be rejected with a structured F5 WARN log."""
    pool = _FakePool()
    caplog.set_level(logging.WARNING, logger="shared.intelligence.interpretation_service")

    # Make sure the env override isn't accidentally set in the test env.
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ALLOW_NULL_ENTRY_PRICE", None)
        result = await create_tracked_position_from_interpretation(
            shared_pool=pool,
            signal_interpretation_id=1,
            news_item_id=10,
            media_item_id=None,
            trader_profile_id=2,
            instrument_symbol="BTC/USDT",
            instrument_exchange="bybit",
            direction="long",
            entry_price=None,
            stop_loss=None,
            take_profit_1=None,
            detection_method="llm_vision",
            detection_confidence=0.85,
            raw_signal_text="LONG BTC bro just trust me",
            correlation_id="cid-rejected",
        )

    assert result is None
    # No INSERT attempted — the guard runs before the SQL.
    assert all("INSERT INTO public.tracked_positions" not in sql for sql, _ in pool.calls), (
        "F5 should reject before reaching the INSERT"
    )
    # The structured WARN must include the correlation_id and signal text
    # so F9's backfill can grep for these.
    assert any("F5 reject tracked_position" in rec.message for rec in caplog.records)
    assert any("cid-rejected" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_zero_entry_price_is_rejected() -> None:
    """entry_price=0.0 is treated the same as NULL — refused."""
    pool = _FakePool()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ALLOW_NULL_ENTRY_PRICE", None)
        result = await create_tracked_position_from_interpretation(
            shared_pool=pool,
            signal_interpretation_id=1,
            news_item_id=10,
            media_item_id=None,
            trader_profile_id=2,
            instrument_symbol="BTC/USDT",
            instrument_exchange="bybit",
            direction="short",
            entry_price=0.0,
            stop_loss=None,
            take_profit_1=None,
            detection_method="llm_vision",
            detection_confidence=0.5,
            raw_signal_text="ambiguous",
        )
    assert result is None
    assert all("INSERT INTO public.tracked_positions" not in sql for sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_negative_entry_price_is_rejected() -> None:
    """Defensive: a stray negative entry must not slip through."""
    pool = _FakePool()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ALLOW_NULL_ENTRY_PRICE", None)
        result = await create_tracked_position_from_interpretation(
            shared_pool=pool,
            signal_interpretation_id=1,
            news_item_id=10,
            media_item_id=None,
            trader_profile_id=2,
            instrument_symbol="BTC/USDT",
            instrument_exchange="bybit",
            direction="long",
            entry_price=-100.0,
            stop_loss=None,
            take_profit_1=None,
            detection_method="llm_vision",
            detection_confidence=0.5,
            raw_signal_text="bad data",
        )
    assert result is None


@pytest.mark.asyncio
async def test_unclear_direction_still_short_circuits_first() -> None:
    """The pre-existing direction guard runs BEFORE F5 — direction='unclear'
    returns None without even evaluating entry_price."""
    pool = _FakePool()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ALLOW_NULL_ENTRY_PRICE", None)
        result = await create_tracked_position_from_interpretation(
            shared_pool=pool,
            signal_interpretation_id=1,
            news_item_id=10,
            media_item_id=None,
            trader_profile_id=2,
            instrument_symbol="BTC/USDT",
            instrument_exchange="bybit",
            direction="unclear",
            entry_price=78000.0,  # valid, but should never matter
            stop_loss=None,
            take_profit_1=None,
            detection_method="llm_vision",
            detection_confidence=0.5,
            raw_signal_text="?",
        )
    assert result is None
    assert pool.calls == []


@pytest.mark.asyncio
async def test_env_override_allows_null_for_backfill() -> None:
    """ALLOW_NULL_ENTRY_PRICE=1 lets F9's backfill script insert + immediately
    backfill in a single transaction without tripping its own guard."""
    pool = _FakePool(returning_id=42)

    # Patch the embed/agreement helpers so the test doesn't reach out to
    # the embedding service or LLM. Using None for entry_reason_trader
    # already short-circuits both, but be explicit.
    with patch.dict(os.environ, {"ALLOW_NULL_ENTRY_PRICE": "1"}, clear=False):
        result = await create_tracked_position_from_interpretation(
            shared_pool=pool,
            signal_interpretation_id=1,
            news_item_id=10,
            media_item_id=None,
            trader_profile_id=2,
            instrument_symbol="BTC/USDT",
            instrument_exchange="bybit",
            direction="long",
            entry_price=None,
            stop_loss=None,
            take_profit_1=None,
            detection_method="backfill_recovery",
            detection_confidence=0.5,
            raw_signal_text="backfill",
            correlation_id="cid-backfill",
        )

    assert result == 42
    # And the INSERT was actually attempted.
    assert any("INSERT INTO public.tracked_positions" in sql for sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_already_in_play_guard() -> None:
    """Already-In-Play / Play-Out Guard must reject already completed/played-out trades."""
    pool = _FakePool(returning_id=999)

    # 1. Test LONG already stopped out (live price 57000.0 <= stop loss 58000.0)
    with patch("shared.intelligence.interpretation_service._ccxt_live_price") as mock_live:
        mock_live.return_value = (57000.0, "BTC/USDT", 1700000000000)
        result = await create_tracked_position_from_interpretation(
            shared_pool=pool,
            signal_interpretation_id=1,
            news_item_id=10,
            media_item_id=None,
            trader_profile_id=2,
            instrument_symbol="BTC/USDT",
            instrument_exchange="bybit",
            direction="long",
            entry_price=60000.0,
            stop_loss=58000.0,
            take_profit_1=70000.0,
            detection_method="llm_vision",
            detection_confidence=0.5,
            raw_signal_text="already dead long",
        )
    assert result is None

    # 2. Test LONG already hit target (live price 71000.0 >= take profit 70000.0)
    with patch("shared.intelligence.interpretation_service._ccxt_live_price") as mock_live:
        mock_live.return_value = (71000.0, "BTC/USDT", 1700000000000)
        result = await create_tracked_position_from_interpretation(
            shared_pool=pool,
            signal_interpretation_id=1,
            news_item_id=10,
            media_item_id=None,
            trader_profile_id=2,
            instrument_symbol="BTC/USDT",
            instrument_exchange="bybit",
            direction="long",
            entry_price=60000.0,
            stop_loss=58000.0,
            take_profit_1=70000.0,
            detection_method="llm_vision",
            detection_confidence=0.5,
            raw_signal_text="already played long",
        )
    assert result is None

    # 3. Test LONG valid/fresh (live price 61000.0, between SL 58000.0 and TP 70000.0)
    with patch("shared.intelligence.interpretation_service._ccxt_live_price") as mock_live:
        mock_live.return_value = (61000.0, "BTC/USDT", 1700000000000)
        result = await create_tracked_position_from_interpretation(
            shared_pool=pool,
            signal_interpretation_id=1,
            news_item_id=10,
            media_item_id=None,
            trader_profile_id=2,
            instrument_symbol="BTC/USDT",
            instrument_exchange="bybit",
            direction="long",
            entry_price=60000.0,
            stop_loss=58000.0,
            take_profit_1=70000.0,
            detection_method="llm_vision",
            detection_confidence=0.5,
            raw_signal_text="valid fresh long",
        )
    assert result == 999
