"""
Tests for the entry-touch activation predicate that replaced the old
single-close ``current_price >= entry`` comparison in
``PositionMonitor._activate_pending_positions`` (2026-05-22 fix).

We exercise the SQL predicate via a tiny in-memory fake pool — no real
Postgres needed — to verify that:

    1. A candle whose ``[low, high]`` range strictly contains ``entry``
       triggers activation regardless of direction or entry style.
    2. Candles with timestamp ``<= position.created_at`` are ignored
       (no retro-fit).
    3. When no candle has touched entry yet, the helper returns ``None``
       so the position stays pending.

Run: ``pytest shared/intelligence/test_activation_predicate.py -v``
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from shared.intelligence import position_monitor as pm


# ---------------------------------------------------------------------------
# Tiny in-memory pool that satisfies the bits position_monitor uses.
# ---------------------------------------------------------------------------
class FakeRow(dict):
    """Subclass of dict so ``row['col']`` works the same as asyncpg.Record."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)


class FakePool:
    """Implements ``fetch_one`` + ``fetch_all`` against a static candle list.

    The fake matches by ``instrument_id`` and ``timeframe`` and filters by
    the ``since`` and entry-range parameters our new SQL passes.
    """

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
        # Symbol/exchange resolution path (fetch_one) → return matching id.
        if "FROM public.instruments" in sql:
            if self._symbol_resolves:
                return FakeRow(id=self._instrument_id)
            return None
        # Candle-touch path → walk fake candles and apply same predicate as SQL.
        if "FROM public.candles" in sql and "low <= $4" in sql:
            # params order: (instrument_id, timeframe, since, entry)
            _iid, _tf, since, entry = params
            for ts, high, low, close in sorted(self._candles):
                if ts <= since:
                    continue
                if low <= entry <= high:
                    return FakeRow(timestamp=ts, high=high, low=low, close=close)
            return None
        return None

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> List[FakeRow]:
        return []

    async def execute(self, sql: str, params: Sequence[Any]) -> None:
        return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@pytest.fixture
def base_ts() -> datetime:
    return datetime(2026, 5, 22, 0, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_long_limit_below_does_NOT_activate_until_price_dips(base_ts):
    """Trader posts a LONG with entry < price-at-signal (buy-the-dip).
    Old code retro-activated immediately. New code must wait for a
    candle whose low actually drops to entry.
    """
    # Created at base_ts. Two later candles: first one DOESN'T touch
    # entry=100; second one drops low to 99 and touches.
    candles = [
        (base_ts + timedelta(minutes=1), 110.0, 105.0, 108.0),
        (base_ts + timedelta(minutes=2), 105.0, 99.0, 101.0),  # touches 100
    ]
    pool = FakePool(instrument_id=7, candles=candles)
    result = await pm._find_entry_touch_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", entry=100.0, since=base_ts,
    )
    assert result is not None
    ts, close = result
    assert ts == base_ts + timedelta(minutes=2)
    assert close == 101.0


@pytest.mark.asyncio
async def test_short_limit_above_does_NOT_activate_until_price_rises(base_ts):
    """Mirror of the above for shorts."""
    # entry=120, price-at-signal was ~105. First candle never reaches 120;
    # second candle high pierces to 121 and touches.
    candles = [
        (base_ts + timedelta(minutes=1), 115.0, 105.0, 110.0),
        (base_ts + timedelta(minutes=2), 121.0, 110.0, 118.0),
    ]
    pool = FakePool(instrument_id=7, candles=candles)
    result = await pm._find_entry_touch_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", entry=120.0, since=base_ts,
    )
    assert result is not None
    ts, close = result
    assert ts == base_ts + timedelta(minutes=2)
    assert close == 118.0


@pytest.mark.asyncio
async def test_candle_at_or_before_created_at_is_ignored(base_ts):
    """A candle that touches entry but happened BEFORE created_at must NOT
    activate (otherwise we'd retro-fit from historical data)."""
    # Candle that touches entry IS at base_ts (== created_at) — must be skipped.
    # Newer candle does not touch.
    candles = [
        (base_ts, 105.0, 95.0, 100.0),                       # would have touched but it's at since
        (base_ts + timedelta(minutes=1), 110.0, 105.0, 108.0),
    ]
    pool = FakePool(instrument_id=7, candles=candles)
    result = await pm._find_entry_touch_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", entry=100.0, since=base_ts,
    )
    assert result is None  # no eligible touch


@pytest.mark.asyncio
async def test_no_candle_touches_returns_none(base_ts):
    candles = [
        (base_ts + timedelta(minutes=1), 110.0, 105.0, 108.0),
        (base_ts + timedelta(minutes=2), 109.0, 106.0, 107.0),
    ]
    pool = FakePool(instrument_id=7, candles=candles)
    result = await pm._find_entry_touch_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", entry=100.0, since=base_ts,
    )
    assert result is None


@pytest.mark.asyncio
async def test_breakout_long_activates_when_high_pierces(base_ts):
    """LONG with entry ABOVE price-at-signal (breakout). Should activate
    when a candle's high reaches/passes entry. (Old code handled this case
    correctly — regression guard.)
    """
    candles = [
        (base_ts + timedelta(minutes=1), 99.0, 95.0, 97.0),       # below entry
        (base_ts + timedelta(minutes=2), 101.5, 99.5, 100.5),     # high pierces 100
    ]
    pool = FakePool(instrument_id=7, candles=candles)
    result = await pm._find_entry_touch_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", entry=100.0, since=base_ts,
    )
    assert result is not None
    ts, _close = result
    assert ts == base_ts + timedelta(minutes=2)


@pytest.mark.asyncio
async def test_unresolved_symbol_returns_none(base_ts):
    pool = FakePool(instrument_id=0, candles=[], symbol_resolves=False)
    result = await pm._find_entry_touch_candle(
        pool=pool, symbol="JUNKSYMBOL", exchange=None,
        timeframe="1m", entry=100.0, since=base_ts,
    )
    assert result is None


@pytest.mark.asyncio
async def test_not_before_cuts_off_stale_touches(base_ts):
    """If ``not_before`` is supplied, candles whose timestamp is older
    than it must NOT activate. Models a monitor restart that should not
    retroactively activate week-old touches."""
    # Touching candle is RIGHT after created_at, but well before not_before.
    candles = [
        (base_ts + timedelta(minutes=1), 105.0, 99.0, 101.0),  # touches, but stale
    ]
    pool = FakePool(instrument_id=7, candles=candles)
    # Position created at base_ts. "Live watcher" window: last 30 minutes
    # from a "now" that's 1 day later.
    fake_now = base_ts + timedelta(days=1)
    not_before = fake_now - timedelta(minutes=30)
    result = await pm._find_entry_touch_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", entry=100.0, since=base_ts, not_before=not_before,
    )
    assert result is None


@pytest.mark.asyncio
async def test_not_before_admits_fresh_touches(base_ts):
    """A touch inside the recency window must still activate."""
    candles = [
        (base_ts + timedelta(minutes=1), 105.0, 99.0, 101.0),
    ]
    pool = FakePool(instrument_id=7, candles=candles)
    # not_before is BEFORE the candle → candle is fresh enough.
    not_before = base_ts  # 0 minutes ago, candle is 1 minute after
    result = await pm._find_entry_touch_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", entry=100.0, since=base_ts, not_before=not_before,
    )
    assert result is not None


@pytest.mark.asyncio
async def test_entry_at_low_or_high_boundary_counts(base_ts):
    """``low <= entry <= high`` is inclusive on both ends. A trader who
    posted entry exactly at the candle's low / high should activate."""
    candles_low_match = [
        (base_ts + timedelta(minutes=1), 105.0, 100.0, 102.0),  # low == entry
    ]
    pool = FakePool(instrument_id=7, candles=candles_low_match)
    result = await pm._find_entry_touch_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", entry=100.0, since=base_ts,
    )
    assert result is not None  # inclusive

    candles_high_match = [
        (base_ts + timedelta(minutes=1), 100.0, 95.0, 98.0),  # high == entry
    ]
    pool = FakePool(instrument_id=7, candles=candles_high_match)
    result = await pm._find_entry_touch_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", entry=100.0, since=base_ts,
    )
    assert result is not None  # inclusive
