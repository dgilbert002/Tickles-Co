"""
Module: test_text_signal_extractor
Purpose: Smoke test for text-based trade signal extraction
Location: /opt/tickles/shared/tests/test_text_signal_extractor.py
"""

import pytest

from shared.intelligence.text_signal_extractor import (
    classify_message_type,
    _passes_prefilter,
)


def test_classify_message_type() -> None:
    """Message classification into trade_setup / commentary / meme / unknown."""
    assert classify_message_type("Buy BTC at 65000 SL 64000 TP 70000") == "trade_setup"
    assert classify_message_type("good morning everyone, thanks for joining") == "commentary"
    assert classify_message_type("🚀🚀🚀 LAMBO") == "meme"
    assert classify_message_type("hello") == "unknown"


def test_prefilter_skips_non_signals() -> None:
    """Fast prefilter rejects obvious non-signals."""
    assert _passes_prefilter("Buy BTC 65000 SL 64000") is True
    assert _passes_prefilter("hello everyone") is False
    assert _passes_prefilter("🚀🚀🚀") is False
