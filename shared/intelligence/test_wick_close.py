"""
Tests for the wick-aware SL/TP close detection
(``position_monitor._find_sl_tp_wick_candle``).

The previous close-only check (``position_quant.check_sl_tp_hit``) would
miss intra-candle wicks that the trader's broker would have filled at.
The wick helper scans every 1m candle since the previous monitor tick
and returns the FIRST candle whose ``[low, high]`` range touched SL or
TP. Same per-candle convention ``copy_trade_monitor`` uses for the
competition bots: SL is evaluated before TP within a single candle.

Run: ``pytest shared/intelligence/test_wick_close.py -v``
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Sequence, Tuple

import pytest

from shared.intelligence import position_monitor as pm


class FakeRow(dict):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)


class FakePool:
    """Minimal pool stub: resolves a fixed instrument id and returns
    the configured candle list, applying the same ``timestamp > since``
    filter the production SQL does."""

    def __init__(
        self,
        *,
        instrument_id: int,
        candles: Sequence[Tuple[datetime, float, float, float]],
        symbol_resolves: bool = True,
    ) -> None:
        # candles: list of (ts, high, low, close).
        self._instrument_id = instrument_id
        self._candles = list(candles)
        self._symbol_resolves = symbol_resolves

    async def fetch_one(self, sql: str, params: Sequence[Any]) -> Optional[FakeRow]:
        if "FROM public.instruments" in sql:
            if self._symbol_resolves:
                return FakeRow(id=self._instrument_id)
            return None
        return None

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> List[FakeRow]:
        # Production SQL: instrument_id=$1, timeframe=$2, since=$3, max_candles=$4
        if "FROM public.candles" not in sql:
            return []
        _iid, _tf, since, _max = params
        out: List[FakeRow] = []
        for ts, high, low, close in sorted(self._candles):
            if ts <= since:
                continue
            out.append(FakeRow(**{"timestamp": ts, "high": high, "low": low, "close": close}))
        return out

    async def execute(self, sql: str, params: Sequence[Any]) -> None:
        return None


@pytest.fixture
def base_ts() -> datetime:
    return datetime(2026, 5, 22, 0, 0, 0, tzinfo=timezone.utc)


# -- LONG side ----------------------------------------------------------------
@pytest.mark.asyncio
async def test_long_wicks_through_sl(base_ts):
    """Long entry=100, SL=95. A 1m candle prints l=94, h=98, c=97.
    The trader's stop would have filled at 95. Bug repro: with the
    close-only check, 97 > 95 → no SL hit. With the wick check, the
    candle's low (94) ≤ 95 → SL fires at 95."""
    candles = [
        (base_ts + timedelta(minutes=1), 98.0, 94.0, 97.0),
    ]
    pool = FakePool(instrument_id=7, candles=candles)
    result = await pm._find_sl_tp_wick_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", direction="long",
        stop_loss=95.0, take_profit=110.0, since=base_ts,
    )
    assert result is not None
    ts, _close, hit_type, hit_price = result
    assert hit_type == "sl"
    assert hit_price == 95.0
    assert ts == base_ts + timedelta(minutes=1)


@pytest.mark.asyncio
async def test_long_wicks_through_tp(base_ts):
    """Long entry=100, TP=110. Candle h=112, l=105, c=107 — TP wicked."""
    candles = [
        (base_ts + timedelta(minutes=1), 112.0, 105.0, 107.0),
    ]
    pool = FakePool(instrument_id=7, candles=candles)
    result = await pm._find_sl_tp_wick_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", direction="long",
        stop_loss=95.0, take_profit=110.0, since=base_ts,
    )
    assert result is not None
    _ts, _close, hit_type, hit_price = result
    assert hit_type == "tp"
    assert hit_price == 110.0


# -- SHORT side ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_short_wicks_through_sl(base_ts):
    """Short entry=100, SL=105. Candle h=106, l=99, c=101 — SL wicked."""
    candles = [
        (base_ts + timedelta(minutes=1), 106.0, 99.0, 101.0),
    ]
    pool = FakePool(instrument_id=7, candles=candles)
    result = await pm._find_sl_tp_wick_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", direction="short",
        stop_loss=105.0, take_profit=90.0, since=base_ts,
    )
    assert result is not None
    _ts, _close, hit_type, hit_price = result
    assert hit_type == "sl"
    assert hit_price == 105.0


@pytest.mark.asyncio
async def test_short_wicks_through_tp(base_ts):
    """Short entry=100, TP=90. Candle h=98, l=89, c=92 — TP wicked."""
    candles = [
        (base_ts + timedelta(minutes=1), 98.0, 89.0, 92.0),
    ]
    pool = FakePool(instrument_id=7, candles=candles)
    result = await pm._find_sl_tp_wick_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", direction="short",
        stop_loss=105.0, take_profit=90.0, since=base_ts,
    )
    assert result is not None
    _ts, _close, hit_type, hit_price = result
    assert hit_type == "tp"
    assert hit_price == 90.0


# -- Same-candle ambiguity (SL wins) ------------------------------------------
@pytest.mark.asyncio
async def test_same_candle_sl_and_tp_long_sl_wins(base_ts):
    """Long entry=100, SL=95, TP=110. Candle h=112 l=94 c=100 — BOTH hit.
    Conservative convention: SL is evaluated first within a candle."""
    candles = [
        (base_ts + timedelta(minutes=1), 112.0, 94.0, 100.0),
    ]
    pool = FakePool(instrument_id=7, candles=candles)
    result = await pm._find_sl_tp_wick_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", direction="long",
        stop_loss=95.0, take_profit=110.0, since=base_ts,
    )
    assert result is not None
    _ts, _close, hit_type, hit_price = result
    assert hit_type == "sl"  # SL wins on ambiguity
    assert hit_price == 95.0


@pytest.mark.asyncio
async def test_same_candle_sl_and_tp_short_sl_wins(base_ts):
    """Mirror of the long case for shorts."""
    candles = [
        (base_ts + timedelta(minutes=1), 106.0, 89.0, 100.0),  # both SL and TP
    ]
    pool = FakePool(instrument_id=7, candles=candles)
    result = await pm._find_sl_tp_wick_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", direction="short",
        stop_loss=105.0, take_profit=90.0, since=base_ts,
    )
    assert result is not None
    _ts, _close, hit_type, hit_price = result
    assert hit_type == "sl"
    assert hit_price == 105.0


# -- No-touch / since-bound ---------------------------------------------------
@pytest.mark.asyncio
async def test_no_wick_returns_none(base_ts):
    candles = [
        (base_ts + timedelta(minutes=1), 105.0, 96.0, 100.0),
        (base_ts + timedelta(minutes=2), 104.0, 97.0, 100.0),
    ]
    pool = FakePool(instrument_id=7, candles=candles)
    result = await pm._find_sl_tp_wick_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", direction="long",
        stop_loss=95.0, take_profit=110.0, since=base_ts,
    )
    assert result is None


@pytest.mark.asyncio
async def test_candle_before_since_is_ignored(base_ts):
    """A candle that wicks SL but is BEFORE the since bound should not
    trigger. Mirrors the per-tick window: we only react to candles that
    appeared between this tick and the last one."""
    candles = [
        # Wicks SL but timestamp == since (not strictly after).
        (base_ts, 98.0, 94.0, 97.0),
        # All subsequent candles are clean.
        (base_ts + timedelta(minutes=1), 100.0, 96.0, 99.0),
    ]
    pool = FakePool(instrument_id=7, candles=candles)
    result = await pm._find_sl_tp_wick_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", direction="long",
        stop_loss=95.0, take_profit=110.0, since=base_ts,
    )
    assert result is None


# -- Edge cases ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_unresolved_symbol_returns_none(base_ts):
    pool = FakePool(instrument_id=0, candles=[], symbol_resolves=False)
    result = await pm._find_sl_tp_wick_candle(
        pool=pool, symbol="JUNK", exchange=None,
        timeframe="1m", direction="long",
        stop_loss=95.0, take_profit=110.0, since=base_ts,
    )
    assert result is None


@pytest.mark.asyncio
async def test_no_sl_or_tp_returns_none(base_ts):
    """If both SL and TP are None there's nothing to detect — skip query."""
    pool = FakePool(instrument_id=7, candles=[])
    result = await pm._find_sl_tp_wick_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", direction="long",
        stop_loss=None, take_profit=None, since=base_ts,
    )
    assert result is None


@pytest.mark.asyncio
async def test_invalid_direction_returns_none(base_ts):
    candles = [(base_ts + timedelta(minutes=1), 98.0, 94.0, 97.0)]
    pool = FakePool(instrument_id=7, candles=candles)
    result = await pm._find_sl_tp_wick_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", direction="sideways",  # invalid
        stop_loss=95.0, take_profit=110.0, since=base_ts,
    )
    assert result is None


@pytest.mark.asyncio
async def test_only_sl_set(base_ts):
    """If only SL is set, TP is None — the TP comparison must be skipped
    without raising. Long candle wicks SL only."""
    candles = [(base_ts + timedelta(minutes=1), 98.0, 94.0, 97.0)]
    pool = FakePool(instrument_id=7, candles=candles)
    result = await pm._find_sl_tp_wick_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", direction="long",
        stop_loss=95.0, take_profit=None, since=base_ts,
    )
    assert result is not None
    assert result[2] == "sl"


@pytest.mark.asyncio
async def test_first_candle_wins(base_ts):
    """If two candles both wick, the first one (oldest) wins."""
    candles = [
        (base_ts + timedelta(minutes=1), 112.0, 100.0, 106.0),  # TP wick first
        (base_ts + timedelta(minutes=2), 96.0, 90.0, 92.0),     # SL wick later
    ]
    pool = FakePool(instrument_id=7, candles=candles)
    result = await pm._find_sl_tp_wick_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", direction="long",
        stop_loss=95.0, take_profit=110.0, since=base_ts,
    )
    assert result is not None
    ts, _close, hit_type, _hit_price = result
    assert hit_type == "tp"
    assert ts == base_ts + timedelta(minutes=1)
