"""
Module: test_text_signal_hardening
Purpose: Test reply block stripping, symbol regex hardening, and database instrument validation.
Location: /opt/tickles/shared/tests/test_text_signal_hardening.py
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from shared.intelligence.text_signal_extractor import (
    strip_reply_prefix,
    extract_signal_from_text,
    classify_message_type,
)
from shared.intelligence.interpretation_service import is_valid_db_instrument


def test_strip_reply_prefix() -> None:
    """Verify that strip_reply_prefix correctly isolates the actual reply message content."""
    # Basic reply quote case
    text_with_reply = "[Reply to @thenagel]: wait what.... u long?\ntook the long yesterday at 1.70"
    assert strip_reply_prefix(text_with_reply) == "took the long yesterday at 1.70"

    # Multi-line actual message
    text_with_reply_multiline = "[Reply to @user]: quote here\nfirst line of reply\nsecond line of reply"
    assert strip_reply_prefix(text_with_reply_multiline) == "first line of reply\nsecond line of reply"

    # No reply quote case (should return original text)
    normal_text = "Standard trade setup for BTC/USDT."
    assert strip_reply_prefix(normal_text) == normal_text

    # Edge cases
    assert strip_reply_prefix("") == ""
    assert strip_reply_prefix(None) == ""


@pytest.mark.asyncio
async def test_extract_signal_with_reply_and_blacklist() -> None:
    """Verify signal extraction handles reply quotes and blacklisted non-ticker words correctly."""
    # 1. REPLY should NOT be extracted as a symbol even in uppercase reply format
    reply_msg = "[Reply to @thenagel]: Started Short position just at 1.70\nWait what... u long?"
    signal = await extract_signal_from_text(reply_msg, use_llm_fallback=False)
    # REPLY is blacklisted, and stripped, so no signal detected
    assert signal is None

    # 2. Extracting valid trade from a message that contains a reply quote
    valid_with_reply = "[Reply to @trader_j]: Long BTC @ 65000\nNo, I am actually long SOL @ 130 with SL 120 and TP 150"
    signal = await extract_signal_from_text(valid_with_reply, use_llm_fallback=False)
    assert signal is not None
    assert signal["symbol"] == "SOL"
    assert signal["direction"] == "long"
    assert signal["entry"] == 130.0

    # 3. Uppercase blacklist words like LONG should not be matched as symbols
    blacklist_msg = "LONG this setup at 100!"
    signal = await extract_signal_from_text(blacklist_msg, use_llm_fallback=False)
    assert signal is None


def test_classify_message_type_with_reply() -> None:
    """Verify that message classification is performed on the stripped text content."""
    # Quoted text is not a meme, but replier's text is a meme
    reply_with_meme = "[Reply to @trader]: BTC Long @ 65000\nlol so funny meme 🤣"
    assert classify_message_type(reply_with_meme) == "meme"

    # Quoted text is a setup, but replier is just saying hi
    reply_with_hi = "[Reply to @trader]: BTC Long SL 64000 TP 70000\ngood morning hi hello"
    assert classify_message_type(reply_with_hi) == "commentary"


@pytest.mark.asyncio
async def test_is_valid_db_instrument() -> None:
    """Verify is_valid_db_instrument returns True only if symbol exists in database public.instruments."""
    mock_pool = MagicMock()
    mock_pool.fetch_one = AsyncMock()

    # Case A: Symbol exists (database returns a row)
    mock_pool.fetch_one.return_value = {"id": 123}
    assert await is_valid_db_instrument(mock_pool, "BTCUSDT") is True

    # Case B: Symbol does not exist (database returns None)
    mock_pool.fetch_one.return_value = None
    assert await is_valid_db_instrument(mock_pool, "REPLY") is False
    assert await is_valid_db_instrument(mock_pool, "UNKNOWN") is False
