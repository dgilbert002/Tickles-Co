"""
Module: test_trade_dedup
Purpose: Smoke test for the trade deduplication module
Location: /opt/tickles/shared/tests/test_trade_dedup.py
"""

import pytest

from shared.intelligence.trade_dedup import is_continuation_text


def test_continuation_text_detection() -> None:
    """Emoji and keyword-based continuation detection."""
    assert is_continuation_text("still in the trade 🟢") is True
    assert is_continuation_text("update: moved SL to 1.20") is True
    assert is_continuation_text("at the trade now") is True
    assert is_continuation_text("new setup on EURUSD") is False
    assert is_continuation_text("random commentary") is False
