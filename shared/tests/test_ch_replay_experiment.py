"""Tests for chart_hacker replay P&L simulation."""

from shared.intelligence.ch_replay_experiment import (
    pick_chart_hacker_trade,
    simulate_trade_pnl,
)


def test_simulate_long_tp_hit() -> None:
    candles = [
        {"open": 100, "high": 101, "low": 99, "close": 100.5},
        {"open": 100.5, "high": 110, "low": 100, "close": 109},
    ]
    out = simulate_trade_pnl(
        direction="long", entry=100, stop_loss=95, take_profit=108, candles=candles,
    )
    assert out["outcome"] == "tp_hit"
    assert out["pnl_pct"] == 8.0


def test_simulate_short_sl_first_on_ambiguous_bar() -> None:
    candles = [{"open": 100, "high": 105, "low": 95, "close": 100}]
    out = simulate_trade_pnl(
        direction="short", entry=100, stop_loss=104, take_profit=90, candles=candles,
    )
    assert out["outcome"] == "sl_hit"


def test_quant_result_from_ohlcv_bullish() -> None:
    from shared.intelligence.interpretation_service import _quant_result_from_ohlcv

    n = 60
    closes = [100.0 + i * 0.5 for i in range(n)]
    highs = [c + 0.2 for c in closes]
    lows = [c - 0.2 for c in closes]
    out = _quant_result_from_ohlcv(closes, highs, lows)
    assert out.indicators["rsi14"] is not None
    assert out.direction in ("long", "neutral", "short")


def test_quant_result_from_ohlcv_too_short() -> None:
    from shared.intelligence.interpretation_service import _quant_result_from_ohlcv

    out = _quant_result_from_ohlcv([100.0], [101.0], [99.0])
    assert out.direction == "unclear"
