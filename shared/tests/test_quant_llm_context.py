"""Unit tests for quant LLM context formatting."""

from shared.intelligence.interpretation_service import QuantResult
from shared.intelligence.quant_llm_context import format_chart_hacker_quant_block


def test_format_chart_hacker_quant_block() -> None:
    quant = QuantResult(
        direction="short",
        confidence=0.55,
        indicators={"rsi14": 72.1, "reasons": ["rsi>70"]},
    )
    block = format_chart_hacker_quant_block(quant)
    assert "chart_hacker ONLY" in block
    assert '"status":"full"' in block.replace(" ", "")
    assert "72.1" in block


def test_is_informative_quant() -> None:
    from shared.intelligence.quant_llm_context import is_informative_quant

    full = QuantResult(direction="long", confidence=0.5, indicators={"rsi14": 55})
    assert is_informative_quant(full) is True
    degraded = QuantResult(
        direction="unclear",
        confidence=0.0,
        indicators={"current_price": 100.0, "degraded": True},
    )
    assert is_informative_quant(degraded) is False
