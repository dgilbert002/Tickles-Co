"""Tests for prefilter ticker normalization and quant symbol resolution."""

from shared.intelligence.interpretation_service import (
    PrefilterResult,
    _normalize_chart_ticker,
    resolve_symbol_for_quant,
    stamp_quant_result_context,
    QuantResult,
)


def test_normalize_tao_perpetual() -> None:
    assert _normalize_chart_ticker("TAO PERPETUAL CONTRACT") == "TAO/USDT"


def test_normalize_bybit_prefix() -> None:
    assert _normalize_chart_ticker("BYBIT:BTCUSDT.P") == "BTC/USDT"


def test_normalize_compound_unknown() -> None:
    assert _normalize_chart_ticker("BTC & ETH") == "UNKNOWN"


def test_normalize_strips_spaced_timeframe() -> None:
    assert _normalize_chart_ticker("HYPEUS 4H") == "HYPEUS/USDT"


def test_normalize_strips_glued_timeframe() -> None:
    assert _normalize_chart_ticker("BTCUSDT1H") == "BTC/USDT"


def test_normalize_slash_with_timeframe() -> None:
    assert _normalize_chart_ticker("TAO/USDT 4H") == "TAO/USDT"


def test_normalize_dominance_rejected() -> None:
    assert _normalize_chart_ticker("USDT.D") == "UNKNOWN"
    assert _normalize_chart_ticker("BTC.D") == "UNKNOWN"


def test_normalize_null_literal_rejected() -> None:
    assert _normalize_chart_ticker("null") == "UNKNOWN"
    assert _normalize_chart_ticker("None") == "UNKNOWN"


def test_resolve_prefilter_wins_over_text() -> None:
    pf = PrefilterResult(
        rejected=False,
        ticker="BTC/USDT",
        ticker_confidence=0.9,
    )
    sym, src = resolve_symbol_for_quant("LINK/USDT", pf)
    assert sym == "BTC/USDT"
    assert src == "prefilter"


def test_resolve_text_when_prefilter_low_confidence() -> None:
    pf = PrefilterResult(
        rejected=False,
        ticker="BTC/USDT",
        ticker_confidence=0.5,
    )
    sym, src = resolve_symbol_for_quant("ETH/USDT", pf)
    assert sym == "ETH/USDT"
    assert src == "text"


def test_resolve_unknown_when_nothing() -> None:
    sym, src = resolve_symbol_for_quant("UNKNOWN", None)
    assert sym == "UNKNOWN"
    assert src == "unknown"


def test_stamp_quant_audit_fields() -> None:
    q = QuantResult(direction="long", confidence=0.5, indicators={"rsi14": 55.0})
    out = stamp_quant_result_context(
        q,
        quant_symbol="BTC/USDT",
        quant_symbol_source="prefilter",
        text_symbol="LINK/USDT",
    )
    assert out.indicators["quant_symbol"] == "BTC/USDT"
    assert out.indicators["quant_symbol_source"] == "prefilter"
    assert out.indicators["quant_text_symbol"] == "LINK/USDT"
