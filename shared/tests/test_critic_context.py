"""
Module: test_critic_context
Purpose: Unit tests for critic_context enrichment helpers.
Location: /opt/tickles/shared/tests/test_critic_context.py
"""

from shared.intelligence.critic_context import (
    build_at_entry_snapshot,
    normalize_candle_timeframe,
    resolve_critic_timeframe,
    serialize_quant,
    trim_chart_analysis,
)


class _FakeQuant:
    direction = "long"
    confidence = 0.55
    indicators = {"rsi14": 58.2, "reasons": ["ema20>ema50"]}


def test_normalize_candle_timeframe_maps_aggregates() -> None:
    assert normalize_candle_timeframe("6h") == "4h"
    assert normalize_candle_timeframe("2h") == "1h"
    assert normalize_candle_timeframe("8h") == "4h"
    assert normalize_candle_timeframe("4h") == "4h"


def test_resolve_critic_timeframe_prefers_position() -> None:
    assert resolve_critic_timeframe("6h", "1h", "scalp") == "4h"


def test_resolve_critic_timeframe_trade_type_fallback() -> None:
    assert resolve_critic_timeframe(None, None, "swing") == "4h"
    assert resolve_critic_timeframe(None, None, "scalp") == "15m"


def test_serialize_quant() -> None:
    out = serialize_quant(_FakeQuant())
    assert out is not None
    assert out["direction"] == "long"
    assert out["indicators"]["rsi14"] == 58.2


def test_trim_chart_analysis() -> None:
    raw = {
        "market_structure": "uptrend",
        "indicators": {"rsi": 60},
        "noise": "drop me",
    }
    out = trim_chart_analysis(raw)
    assert out == {"market_structure": "uptrend", "indicators": {"rsi": 60}}


def test_build_at_entry_snapshot() -> None:
    row = {
        "interp_id": 42,
        "interp_timeframe": "4h",
        "quant_direction": "long",
        "quant_confidence": 0.6,
        "quant_indicators": {"rsi14": 55},
        "consensus_direction": "long",
        "consensus_confidence": 0.7,
        "consensus_method": "weighted_average",
        "ai_agreement_score": 0.8,
        "ai_comment": "Looks aligned",
        "chart_analysis": {"market_structure": "range"},
    }
    snap = build_at_entry_snapshot(row)
    assert snap is not None
    assert snap["interpretation_id"] == 42
    assert snap["quant"]["direction"] == "long"
    assert snap["consensus"]["method"] == "weighted_average"
    assert snap["chart_analysis"]["market_structure"] == "range"
