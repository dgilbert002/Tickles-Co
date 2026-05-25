"""
Module: test_radar_live_price_fallback
Purpose: Unit tests for the entry-radar live-price fallback added in
         Round 13 (2026-05-24). When the local candle store is empty
         for a symbol, the radar handler must call
         :func:`fetch_live_price` and back-fill ``current_price`` +
         ``distance_to_entry_pct``.

Location: /opt/tickles/shared/tests/test_radar_live_price_fallback.py

Run:
    python3 -m pytest shared/tests/test_radar_live_price_fallback.py -v
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.dashboard.market_routes import (
    _enrich_radar_with_live_prices,
    _radar_cache_clear,
)


@pytest.fixture(autouse=True)
def _clear_radar_cache():
    """Make sure the module-level cache doesn't leak between tests."""
    _radar_cache_clear()
    yield
    _radar_cache_clear()


def _make_row(
    *,
    symbol: str = "BITTENSOR/USDT:USDT",
    exchange: str = "bybit",
    entry: float = 258.0,
    current: float | None = None,
) -> dict:
    return {
        "symbol": symbol,
        "instrument_exchange": exchange,
        "entry_price": entry,
        "current_price": current,
        "distance_to_entry_pct": None,
    }


class _FakeLPResult:
    """Mimics shared.market_data.live_price.LivePriceResult."""

    def __init__(self, price: float, *, symbol: str = "x", exchange: str = "bybit") -> None:
        self.price = price
        self.symbol = symbol
        self.exchange = exchange
        self.ts_ms = 0


@pytest.mark.asyncio
async def test_no_rows_to_enrich_returns_zero() -> None:
    """When every row already has current_price, do nothing."""
    rows = [_make_row(current=100.0), _make_row(current=200.0)]
    with patch(
        "shared.market_data.live_price.fetch_live_price",
        new_callable=AsyncMock,
    ) as mock_fetch:
        filled = await _enrich_radar_with_live_prices(rows)
    assert filled == 0
    mock_fetch.assert_not_called()


@pytest.mark.asyncio
async def test_skips_row_without_entry_price() -> None:
    """Rows missing entry_price cannot have a meaningful distance."""
    rows = [_make_row(entry=None)]  # type: ignore[arg-type]
    with patch(
        "shared.market_data.live_price.fetch_live_price",
        new_callable=AsyncMock,
    ) as mock_fetch:
        filled = await _enrich_radar_with_live_prices(rows)
    assert filled == 0
    mock_fetch.assert_not_called()


@pytest.mark.asyncio
async def test_skips_unsupported_exchange_silently() -> None:
    """Capital.com isn't in SUPPORTED_EXCHANGES — must skip without raising."""
    rows = [_make_row(exchange="capital.com")]
    with patch(
        "shared.market_data.live_price.fetch_live_price",
        new_callable=AsyncMock,
    ) as mock_fetch:
        filled = await _enrich_radar_with_live_prices(rows)
    assert filled == 0
    mock_fetch.assert_not_called()
    # Row must be unchanged (still no current_price)
    assert rows[0]["current_price"] is None


@pytest.mark.asyncio
async def test_happy_path_fills_one_row() -> None:
    """Single row with empty candles must be filled from live_price."""
    rows = [_make_row()]
    with patch(
        "shared.market_data.live_price.fetch_live_price",
        new_callable=AsyncMock,
        return_value=_FakeLPResult(259.5),
    ):
        filled = await _enrich_radar_with_live_prices(rows)
    assert filled == 1
    assert rows[0]["current_price"] == 259.5
    # entry=258, current=259.5 → +0.581%
    assert rows[0]["distance_to_entry_pct"] == pytest.approx((259.5 - 258.0) / 258.0 * 100.0, rel=1e-6)
    assert rows[0]["path_state"]["price_source"] == "live_ccxt_fallback"


@pytest.mark.asyncio
async def test_short_direction_distance_uses_signed_diff() -> None:
    """Signed difference is correct regardless of direction (the UI
    formats the sign + colour). We only assert the magnitude+sign here."""
    rows = [_make_row(entry=100.0)]
    with patch(
        "shared.market_data.live_price.fetch_live_price",
        new_callable=AsyncMock,
        return_value=_FakeLPResult(95.0),
    ):
        await _enrich_radar_with_live_prices(rows)
    # current 95 < entry 100 → distance = -5%
    assert rows[0]["distance_to_entry_pct"] == pytest.approx(-5.0, rel=1e-6)


@pytest.mark.asyncio
async def test_partial_failure_does_not_block() -> None:
    """If one symbol fails, the other must still be filled."""
    rows = [
        _make_row(symbol="GOOD/USDT:USDT", entry=100.0),
        _make_row(symbol="BAD/USDT:USDT", entry=200.0),
    ]
    from shared.market_data.live_price import LivePriceError

    async def side_effect(symbol, exchange, *, timeout_s):  # noqa: ARG001
        if "GOOD" in symbol:
            return _FakeLPResult(101.0)
        raise LivePriceError("boom")

    with patch(
        "shared.market_data.live_price.fetch_live_price",
        side_effect=side_effect,
    ):
        filled = await _enrich_radar_with_live_prices(rows)
    assert filled == 1
    assert rows[0]["current_price"] == 101.0
    assert rows[1]["current_price"] is None  # untouched on failure


@pytest.mark.asyncio
async def test_zero_or_negative_price_rejected() -> None:
    """A bad upstream price (0 or negative) must not poison the row."""
    rows = [_make_row()]
    with patch(
        "shared.market_data.live_price.fetch_live_price",
        new_callable=AsyncMock,
        return_value=_FakeLPResult(0.0),
    ):
        filled = await _enrich_radar_with_live_prices(rows)
    assert filled == 0
    assert rows[0]["current_price"] is None


@pytest.mark.asyncio
async def test_timeout_handled() -> None:
    """asyncio.TimeoutError must be swallowed, row left alone."""
    import asyncio

    rows = [_make_row()]
    with patch(
        "shared.market_data.live_price.fetch_live_price",
        side_effect=asyncio.TimeoutError("slow"),
    ):
        filled = await _enrich_radar_with_live_prices(rows)
    assert filled == 0
    assert rows[0]["current_price"] is None


@pytest.mark.asyncio
async def test_dedupes_duplicate_symbols() -> None:
    """Three rows on the same symbol should trigger ONE CCXT probe."""
    rows = [
        _make_row(symbol="BTC/USDT:USDT", entry=70000.0),
        _make_row(symbol="BTC/USDT:USDT", entry=72000.0),
        _make_row(symbol="BTC/USDT:USDT", entry=68000.0),
    ]
    with patch(
        "shared.market_data.live_price.fetch_live_price",
        new_callable=AsyncMock,
        return_value=_FakeLPResult(70500.0),
    ) as mock_fetch:
        filled = await _enrich_radar_with_live_prices(rows)
    assert filled == 3
    assert mock_fetch.call_count == 1  # dedup worked
    # Each row got its OWN distance computed against ITS entry
    assert rows[0]["distance_to_entry_pct"] == pytest.approx(
        (70500.0 - 70000.0) / 70000.0 * 100.0, rel=1e-6
    )
    assert rows[2]["distance_to_entry_pct"] == pytest.approx(
        (70500.0 - 68000.0) / 68000.0 * 100.0, rel=1e-6
    )


@pytest.mark.asyncio
async def test_cache_hit_avoids_second_probe() -> None:
    """Two consecutive calls for the same symbol must use the cache."""
    rows1 = [_make_row(symbol="ETH/USDT:USDT", entry=2000.0)]
    rows2 = [_make_row(symbol="ETH/USDT:USDT", entry=2050.0)]
    with patch(
        "shared.market_data.live_price.fetch_live_price",
        new_callable=AsyncMock,
        return_value=_FakeLPResult(2100.0),
    ) as mock_fetch:
        await _enrich_radar_with_live_prices(rows1)
        await _enrich_radar_with_live_prices(rows2)
    assert mock_fetch.call_count == 1  # second call hit cache
    assert rows2[0]["current_price"] == 2100.0


@pytest.mark.asyncio
async def test_negative_cache_avoids_repeated_failures() -> None:
    """A failed probe is cached so the radar doesn't re-stall."""
    from shared.market_data.live_price import LivePriceError

    rows1 = [_make_row(symbol="BITTENSOR/USDT:USDT", entry=258.0)]
    rows2 = [_make_row(symbol="BITTENSOR/USDT:USDT", entry=260.0)]
    with patch(
        "shared.market_data.live_price.fetch_live_price",
        side_effect=LivePriceError("not on bybit"),
    ) as mock_fetch:
        await _enrich_radar_with_live_prices(rows1)
        await _enrich_radar_with_live_prices(rows2)
    # Only ONE upstream probe — second call was satisfied by negative cache
    assert mock_fetch.call_count == 1
    # Both rows correctly remain unfilled
    assert rows1[0]["current_price"] is None
    assert rows2[0]["current_price"] is None
