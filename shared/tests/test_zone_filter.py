"""
Module: test_zone_filter
Purpose: Unit tests for Phase 9 zone filter wrapper.
Location: /opt/tickles/shared/tests/test_zone_filter.py
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from shared.intelligence.zone_filter import classify, passes, _THRESHOLD


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_classify_returns_valid_dict() -> None:
    """classify() returns expected keys when LLM responds."""
    mock_response = {
        "content": '{"is_trading_signal": true, "confidence": 0.85, "reason": "Long BTC call"}',
        "model": "google/gemini-2.5-flash",
        "usage": {"prompt_tokens": 120, "completion_tokens": 40},
    }
    mock_cfg = type("Cfg", (), {"gateway": "requesty"})()

    with patch(
        "shared.intelligence.zone_filter._load_zone_filter_prompt",
        new=AsyncMock(return_value="classify trading signals"),
    ):
        with patch(
            "shared.intelligence.zone_filter.resolve_slot_gateway",
            new=AsyncMock(return_value=(mock_cfg, "google/gemini-2.5-flash")),
        ):
            with patch(
                "shared.intelligence.zone_filter.chat_completion",
                new=AsyncMock(return_value=mock_response),
            ):
                with patch(
                    "shared.intelligence.zone_filter.log_api_call",
                    new=AsyncMock(),
                ):
                    result = await classify(
                        "Long BTC here", source_id=1, correlation_id="test-1"
                    )

    assert result["is_trading_signal"] is True
    assert result["confidence"] == 0.85
    assert result["reason"] == "Long BTC call"


@pytest.mark.anyio
async def test_classify_fail_open_on_timeout() -> None:
    """Timeout returns fail-open dict with is_trading_signal=True."""
    with patch(
        "shared.intelligence.zone_filter._load_zone_filter_prompt",
        new=AsyncMock(return_value="prompt"),
    ):
        with patch(
            "shared.intelligence.zone_filter.resolve_slot_gateway",
            new=AsyncMock(return_value=(type("C", (), {"gateway": "requesty"})(), "m")),
        ):
            with patch(
                "shared.intelligence.zone_filter.chat_completion",
                new=AsyncMock(side_effect=asyncio.TimeoutError),
            ):
                result = await classify("meme", source_id=1, correlation_id="test-2")

    assert result["is_trading_signal"] is True
    assert result["confidence"] == 0.0
    assert "TimeoutError" in result["reason"]


@pytest.mark.anyio
async def test_classify_fail_open_on_exception() -> None:
    """Generic exception returns fail-open dict."""
    with patch(
        "shared.intelligence.zone_filter._load_zone_filter_prompt",
        new=AsyncMock(return_value="prompt"),
    ):
        with patch(
            "shared.intelligence.zone_filter.resolve_slot_gateway",
            new=AsyncMock(return_value=(type("C", (), {"gateway": "requesty"})(), "m")),
        ):
            with patch(
                "shared.intelligence.zone_filter.chat_completion",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ):
                result = await classify("meme", source_id=1, correlation_id="test-3")

    assert result["is_trading_signal"] is True
    assert result["confidence"] == 0.0
    assert "RuntimeError" in result["reason"]


def test_passes_with_high_confidence() -> None:
    """Signal with confidence above threshold passes."""
    zone = {"is_trading_signal": True, "confidence": 0.8, "reason": "Long BTC"}
    assert passes(zone, per_source_threshold=None) is True


def test_passes_below_threshold() -> None:
    """Signal below threshold does not pass."""
    zone = {"is_trading_signal": True, "confidence": 0.3, "reason": "Weak signal"}
    assert passes(zone, per_source_threshold=None) is False


def test_passes_non_signal() -> None:
    """Non-signal does not pass regardless of confidence."""
    zone = {"is_trading_signal": False, "confidence": 0.99, "reason": "News link"}
    assert passes(zone, per_source_threshold=None) is False


def test_passes_fail_open_unavailable() -> None:
    """Fail-open result (unavailable) always passes."""
    zone = {
        "is_trading_signal": True,
        "confidence": 0.0,
        "reason": "zone_filter_unavailable: TimeoutError",
    }
    assert passes(zone, per_source_threshold=None) is True


def test_passes_per_source_override() -> None:
    """Per-source threshold overrides global default."""
    zone = {"is_trading_signal": True, "confidence": 0.5, "reason": "Mid signal"}
    assert passes(zone, per_source_threshold=0.4) is True
    assert passes(zone, per_source_threshold=0.6) is False
