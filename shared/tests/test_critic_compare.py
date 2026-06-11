"""Unit tests for critic_compare pure helpers."""

from shared.intelligence.critic_compare import (
    build_comparison_row,
    compute_pnl_pct,
    quant_aligns_trader,
    summarize_comparisons,
)


def test_compute_pnl_pct_long() -> None:
    assert compute_pnl_pct("long", 100.0, 103.0) == 3.0


def test_quant_aligns_trader() -> None:
    assert quant_aligns_trader("long", "long") is True
    assert quant_aligns_trader("short", "long") is False
    assert quant_aligns_trader("neutral", "long") is None


def test_build_comparison_row_verdict_flip() -> None:
    row = {
        "position_id": 1,
        "actor_id": "jarvais_trader_1",
        "actor_type": "agent",
        "direction": "long",
        "instrument_symbol": "BTC/USDT:USDT",
        "instrument_exchange": "bybit",
        "entry_price": 100,
        "current_price": 98,
        "stop_loss": 90,
        "take_profit": 120,
        "entry_reason_trader": "",
        "stored_would_take": False,
        "stored_confidence": 0.4,
        "stored_memo": "old",
        "stored_sl": None,
        "stored_tp": None,
        "stored_opinion_at": None,
        "status": "open",
    }
    enrichment = {
        "quant_now": {"direction": "long", "confidence": 0.5, "indicators": {"rsi14": 52}},
        "trader_stats_30d": {"accuracy_pct": 65.0},
    }
    opinion = {
        "would_take_trade": True,
        "memo_confidence": 0.72,
        "memo": "RSI neutral, R:R ok",
        "suggested_sl": 91,
        "suggested_tp": 115,
    }
    out = build_comparison_row(row, enrichment, opinion)
    assert out["fresh_critic"]["would_take_trade"] is True
    assert out["comparison"]["verdict_changed_from_stored"] is True
    assert out["comparison"]["trader_accuracy_30d"] == 65.0


def test_summarize_comparisons() -> None:
    items = [
        {"fresh_critic": {"would_take_trade": True}, "comparison": {"verdict_changed_from_stored": True, "quant_aligns_trader": True}},
        {"error": "fail"},
    ]
    s = summarize_comparisons(items)
    assert s["succeeded"] == 1
    assert s["errors"] == 1
    assert s["fresh_would_take"] == 1
    assert s["verdict_flips"] == 1
