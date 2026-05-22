"""
Tests: backtest engine ≡ live position-monitor predicates (Rule 1).

Why this test exists
====================
CONTEXT_V3 says "backtests must equal live". Tonight (2026-05-22) we
fixed two predicate-shaped bugs in the live ``position_monitor``:

  1. Entry activation must use the 1m candle's [low, high] range, not
     a single close. (``_find_entry_touch_candle``)
  2. SL/TP close must use the 1m candle's [low, high] range, not a
     single close. (``_find_sl_tp_wick_candle``)

The ``discrete_backtest`` engine already walks candles using the SAME
predicates. This file is the cheap, deterministic check that locks
the two implementations together. If anyone ever changes one side
without the other (or introduces a new engine that quietly disagrees),
these tests fail in CI.

What "parity" means here in plain English
-----------------------------------------
Given the SAME 1-minute candles and the SAME signal (direction, entry,
SL, TP), both implementations must say the same thing about:

  - Which candle the entry was hit at
  - Which candle the SL or TP was hit at
  - Whether SL or TP fired first (same-candle ambiguity)

If they disagree, a strategy that backtested as profitable could lose
money live, or vice versa — that's the bug this test stops cold.

Run: ``pytest shared/backtest/test_live_parity.py -v``
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Sequence, Tuple

import pytest

from shared.intelligence import position_monitor as pm


# ---------------------------------------------------------------------------
# Reference walker — the canonical predicate.
# Both the live monitor AND the backtest engine MUST agree with this.
# ---------------------------------------------------------------------------
@dataclass
class WalkResult:
    activation_ts: Optional[datetime]   # When entry first touched
    exit_ts: Optional[datetime]         # When SL or TP first touched
    exit_kind: Optional[str]            # 'sl' | 'tp' | None
    exit_level: Optional[float]         # The SL or TP price that hit


def reference_walk(
    candles: Sequence[dict],
    *,
    direction: str,
    entry: float,
    stop_loss: Optional[float],
    take_profit: Optional[float],
    since: datetime,
) -> WalkResult:
    """Pure reference implementation of the entry + SL/TP predicates.

    Rule:
      - Activation: FIRST candle with timestamp > since whose
        [low, high] range contains ``entry``.
      - Close: starting from the candle AFTER activation, the FIRST
        candle whose range touches SL or TP. SL beats TP on same-candle
        ambiguity (conservative trader convention).
    """
    activation_ts: Optional[datetime] = None
    activation_idx: int = -1
    for i, c in enumerate(candles):
        if c["timestamp"] <= since:
            continue
        lo = float(c["low"])
        hi = float(c["high"])
        if lo <= entry <= hi:
            activation_ts = c["timestamp"]
            activation_idx = i
            break
    if activation_ts is None:
        return WalkResult(None, None, None, None)

    # Scan candles strictly AFTER the activation candle.
    for c in candles[activation_idx + 1:]:
        lo = float(c["low"])
        hi = float(c["high"])
        if direction == "long":
            if stop_loss is not None and lo <= stop_loss:
                return WalkResult(activation_ts, c["timestamp"], "sl", float(stop_loss))
            if take_profit is not None and hi >= take_profit:
                return WalkResult(activation_ts, c["timestamp"], "tp", float(take_profit))
        else:  # short
            if stop_loss is not None and hi >= stop_loss:
                return WalkResult(activation_ts, c["timestamp"], "sl", float(stop_loss))
            if take_profit is not None and lo <= take_profit:
                return WalkResult(activation_ts, c["timestamp"], "tp", float(take_profit))
    return WalkResult(activation_ts, None, None, None)


# ---------------------------------------------------------------------------
# Fake pool that lets us drive the live predicates from a candle list.
# Mirrors the FakePool used in test_activation_predicate.py /
# test_wick_close.py — extracted here so the parity test is self-contained.
# ---------------------------------------------------------------------------
class FakeRow(dict):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)


class FakePool:
    """Minimal asyncpg-shaped pool. Returns the configured candle list,
    filtered by ``timestamp > since`` (matching the production SQL)."""

    def __init__(
        self,
        *,
        instrument_id: int,
        candles: Sequence[dict],
    ) -> None:
        self._instrument_id = instrument_id
        # candle dicts: {timestamp, high, low, close}
        self._candles = list(candles)

    async def fetch_one(self, sql: str, params: Sequence[Any]) -> Optional[FakeRow]:
        if "FROM public.instruments" in sql:
            return FakeRow(id=self._instrument_id)
        if "FROM public.candles" in sql and "low <= $4" in sql:
            # Entry-touch path: returns FIRST candle whose range contains entry.
            _iid, _tf, since, entry = params
            for c in sorted(self._candles, key=lambda x: x["timestamp"]):
                if c["timestamp"] <= since:
                    continue
                lo = float(c["low"]); hi = float(c["high"])
                if lo <= entry <= hi:
                    return FakeRow(timestamp=c["timestamp"], high=hi, low=lo, close=float(c["close"]))
            return None
        return None

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> List[FakeRow]:
        # Wick-check path: returns ALL candles after `since`.
        if "FROM public.candles" not in sql:
            return []
        # Params: (instrument_id, timeframe, since, max_candles)
        _iid, _tf, since = params[0], params[1], params[2]
        out: List[FakeRow] = []
        for c in sorted(self._candles, key=lambda x: x["timestamp"]):
            if c["timestamp"] <= since:
                continue
            out.append(FakeRow(
                timestamp=c["timestamp"],
                high=float(c["high"]),
                low=float(c["low"]),
                close=float(c["close"]),
            ))
        return out

    async def execute(self, sql: str, params: Sequence[Any]) -> None:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _candle(ts: datetime, o: float, h: float, l: float, c: float) -> dict:
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c}


async def _live_walk(
    candles: Sequence[dict],
    *,
    direction: str,
    entry: float,
    stop_loss: Optional[float],
    take_profit: Optional[float],
    since: datetime,
) -> WalkResult:
    """Drive the LIVE position_monitor predicates through the same scenario.

    Returns the same WalkResult shape as ``reference_walk`` for direct
    comparison.
    """
    pool = FakePool(instrument_id=7, candles=candles)
    triggered = await pm._find_entry_touch_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", entry=entry, since=since,
    )
    if triggered is None:
        return WalkResult(None, None, None, None)
    activation_ts, _act_close = triggered
    wick = await pm._find_sl_tp_wick_candle(
        pool=pool, symbol="BTC/USDT", exchange="bybit",
        timeframe="1m", direction=direction,
        stop_loss=stop_loss, take_profit=take_profit,
        since=activation_ts,  # strictly AFTER the activation candle
    )
    if wick is None:
        return WalkResult(activation_ts, None, None, None)
    exit_ts, _close, kind, level = wick
    return WalkResult(activation_ts, exit_ts, kind, float(level))


# ---------------------------------------------------------------------------
# Scenario fixtures — each is the same candle stream from base_ts.
# ---------------------------------------------------------------------------
@pytest.fixture
def base_ts() -> datetime:
    return datetime(2026, 5, 22, 0, 0, 0, tzinfo=timezone.utc)


def _ts(base_ts: datetime, m: int) -> datetime:
    return base_ts + timedelta(minutes=m)


# ---------------------------------------------------------------------------
# Parity tests — each asserts live ≡ reference (which the backtest engine
# also implements). If any test fails, the live and the backtest have
# diverged on this scenario.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_parity_long_limit_below_then_tp(base_ts):
    """Trader posts a LONG with entry=100 while price was at 105.
    Price drops to 100, then climbs to 110 (TP). Both engines should
    say: activate at the dip, exit at the TP wick."""
    candles = [
        _candle(_ts(base_ts, 1), 105, 106, 104, 105),
        _candle(_ts(base_ts, 2), 105, 105, 99, 102),   # dips to entry (range 99-105 contains 100)
        _candle(_ts(base_ts, 3), 102, 108, 101, 107),
        _candle(_ts(base_ts, 4), 107, 112, 106, 109),  # high 112 ≥ TP=110
    ]
    sig = dict(direction="long", entry=100.0, stop_loss=95.0, take_profit=110.0, since=base_ts)
    ref = reference_walk(candles, **sig)
    live = await _live_walk(candles, **sig)
    assert ref.activation_ts == live.activation_ts == _ts(base_ts, 2)
    assert ref.exit_ts == live.exit_ts == _ts(base_ts, 4)
    assert ref.exit_kind == live.exit_kind == "tp"
    assert ref.exit_level == live.exit_level == 110.0


@pytest.mark.asyncio
async def test_parity_long_breakout_then_sl(base_ts):
    """LONG breakout: entry=110 (above current 105). Price rallies to
    111 (entry hit), then sells off to 94 (SL=95 wicked)."""
    candles = [
        _candle(_ts(base_ts, 1), 105, 106, 104, 105),
        _candle(_ts(base_ts, 2), 105, 111, 105, 109),  # breaks 110
        _candle(_ts(base_ts, 3), 109, 110, 100, 102),
        _candle(_ts(base_ts, 4), 102, 103, 94, 96),    # low 94 ≤ SL=95
    ]
    sig = dict(direction="long", entry=110.0, stop_loss=95.0, take_profit=125.0, since=base_ts)
    ref = reference_walk(candles, **sig)
    live = await _live_walk(candles, **sig)
    assert ref.activation_ts == live.activation_ts == _ts(base_ts, 2)
    assert ref.exit_ts == live.exit_ts == _ts(base_ts, 4)
    assert ref.exit_kind == live.exit_kind == "sl"
    assert ref.exit_level == live.exit_level == 95.0


@pytest.mark.asyncio
async def test_parity_short_limit_above_then_tp(base_ts):
    """SHORT with entry=100, current 95. Price rallies to 101 (entry hit),
    then drops to 89 (TP=90 wicked)."""
    candles = [
        _candle(_ts(base_ts, 1), 95, 96, 94, 95),
        _candle(_ts(base_ts, 2), 95, 101, 95, 99),     # range 95-101 contains 100
        _candle(_ts(base_ts, 3), 99, 100, 92, 94),
        _candle(_ts(base_ts, 4), 94, 95, 89, 90),      # low 89 ≤ TP=90
    ]
    sig = dict(direction="short", entry=100.0, stop_loss=105.0, take_profit=90.0, since=base_ts)
    ref = reference_walk(candles, **sig)
    live = await _live_walk(candles, **sig)
    assert ref.activation_ts == live.activation_ts == _ts(base_ts, 2)
    assert ref.exit_ts == live.exit_ts == _ts(base_ts, 4)
    assert ref.exit_kind == live.exit_kind == "tp"
    assert ref.exit_level == live.exit_level == 90.0


@pytest.mark.asyncio
async def test_parity_short_breakdown_then_sl(base_ts):
    """SHORT breakdown: entry=90 (below current 95). Price drops to 89
    (entry hit) then rallies to 106 (SL=105 wicked)."""
    candles = [
        _candle(_ts(base_ts, 1), 95, 96, 94, 95),
        _candle(_ts(base_ts, 2), 95, 95, 89, 92),      # range contains 90
        _candle(_ts(base_ts, 3), 92, 100, 91, 99),
        _candle(_ts(base_ts, 4), 99, 106, 98, 104),    # high 106 ≥ SL=105
    ]
    sig = dict(direction="short", entry=90.0, stop_loss=105.0, take_profit=80.0, since=base_ts)
    ref = reference_walk(candles, **sig)
    live = await _live_walk(candles, **sig)
    assert ref.activation_ts == live.activation_ts == _ts(base_ts, 2)
    assert ref.exit_ts == live.exit_ts == _ts(base_ts, 4)
    assert ref.exit_kind == live.exit_kind == "sl"


@pytest.mark.asyncio
async def test_parity_same_candle_sl_and_tp_sl_wins(base_ts):
    """LONG entry=100, SL=95, TP=110. Activation candle: range 99-100.
    Next candle: l=94 h=111 c=102 — BOTH SL and TP hit in the same
    candle. Convention (live AND backtest): SL wins."""
    candles = [
        _candle(_ts(base_ts, 1), 102, 102, 99, 100),    # activation
        _candle(_ts(base_ts, 2), 100, 111, 94, 102),    # both hit
    ]
    sig = dict(direction="long", entry=100.0, stop_loss=95.0, take_profit=110.0, since=base_ts)
    ref = reference_walk(candles, **sig)
    live = await _live_walk(candles, **sig)
    assert ref.exit_kind == live.exit_kind == "sl"
    assert ref.exit_level == live.exit_level == 95.0


@pytest.mark.asyncio
async def test_parity_no_activation(base_ts):
    """Entry never hit — both should report no activation, no exit."""
    candles = [
        _candle(_ts(base_ts, 1), 105, 106, 104, 105),
        _candle(_ts(base_ts, 2), 105, 107, 103, 106),
    ]
    sig = dict(direction="long", entry=100.0, stop_loss=95.0, take_profit=110.0, since=base_ts)
    ref = reference_walk(candles, **sig)
    live = await _live_walk(candles, **sig)
    assert ref.activation_ts is None
    assert live.activation_ts is None


@pytest.mark.asyncio
async def test_parity_activation_but_no_exit(base_ts):
    """Activates but neither SL nor TP touched — both report exit=None."""
    candles = [
        _candle(_ts(base_ts, 1), 102, 102, 99, 100),    # activation
        _candle(_ts(base_ts, 2), 100, 104, 99, 102),    # no touch
        _candle(_ts(base_ts, 3), 102, 103, 100, 101),   # no touch
    ]
    sig = dict(direction="long", entry=100.0, stop_loss=95.0, take_profit=110.0, since=base_ts)
    ref = reference_walk(candles, **sig)
    live = await _live_walk(candles, **sig)
    assert ref.activation_ts == live.activation_ts == _ts(base_ts, 1)
    assert ref.exit_ts is None
    assert live.exit_ts is None


@pytest.mark.asyncio
async def test_parity_only_sl_set_no_tp(base_ts):
    """Some traders post charts with only a stop-loss, no take-profit.
    Both engines must handle the missing TP without raising and close
    on SL wick only."""
    candles = [
        _candle(_ts(base_ts, 1), 102, 102, 99, 100),
        _candle(_ts(base_ts, 2), 100, 102, 94, 96),
    ]
    sig = dict(direction="long", entry=100.0, stop_loss=95.0, take_profit=None, since=base_ts)
    ref = reference_walk(candles, **sig)
    live = await _live_walk(candles, **sig)
    assert ref.exit_kind == live.exit_kind == "sl"


@pytest.mark.asyncio
async def test_parity_strict_inequality_at_since(base_ts):
    """A candle exactly AT ``since`` must NOT be considered for activation
    (mirrors the recency-bound + anti-retro-fit rule). Both engines must
    ignore the base_ts candle even if it touches entry."""
    candles = [
        _candle(base_ts, 102, 102, 99, 100),          # at since — must be skipped
        _candle(_ts(base_ts, 1), 100, 104, 100, 102),  # no touch (low == 100 inclusive though)
    ]
    sig = dict(direction="long", entry=100.0, stop_loss=95.0, take_profit=110.0, since=base_ts)
    ref = reference_walk(candles, **sig)
    live = await _live_walk(candles, **sig)
    # First eligible candle (minute 1) has l=100 which is the boundary;
    # both treat low ≤ entry ≤ high as inclusive, so both activate at +1m.
    assert ref.activation_ts == live.activation_ts == _ts(base_ts, 1)


@pytest.mark.asyncio
async def test_parity_boundary_high_equals_entry(base_ts):
    """The predicate is INCLUSIVE on both bounds: ``low <= entry <= high``.
    A candle whose high exactly equals entry must activate in both."""
    candles = [
        _candle(_ts(base_ts, 1), 95, 100, 95, 98),  # high == entry
    ]
    sig = dict(direction="long", entry=100.0, stop_loss=90.0, take_profit=110.0, since=base_ts)
    ref = reference_walk(candles, **sig)
    live = await _live_walk(candles, **sig)
    assert ref.activation_ts == live.activation_ts == _ts(base_ts, 1)


# ---------------------------------------------------------------------------
# End-to-end parity against the REAL discrete_backtest engine
# ---------------------------------------------------------------------------
# The tests above prove: live monitor ≡ reference walker.
# These tests prove: discrete_backtest engine ≡ reference walker.
# By transitivity: live ≡ backtest. If anyone changes either engine
# without updating the other, one of these test groups will fail.
# ---------------------------------------------------------------------------

def _run_backtest_get_exit(
    candles: List[dict], *, direction: str, entry: float,
    stop_loss: float, take_profit: float, activation_ts: datetime,
) -> Tuple[Optional[str], Optional[datetime]]:
    """Invoke the real run_discrete_backtest on a scenario and pull out
    the (exit_reason, exit_ts) of the single resulting trade.

    Slippage / fees are zeroed out so we can compare exit semantics
    cleanly against the reference walker (which is fee-agnostic).
    """
    from shared.backtest.discrete_backtest import (
        DiscreteTradeSpec, run_discrete_backtest,
    )
    from shared.backtest.engine import BacktestConfig

    cfg = BacktestConfig(
        symbol="BTC/USDT",
        source="parity-test",
        timeframe="1m",
        start_date=candles[0]["timestamp"].isoformat(),
        end_date=candles[-1]["timestamp"].isoformat(),
        initial_capital=10000.0,
        fee_taker_bps=0.0, slippage_bps=0.0, funding_bps_per_8h=0.0,
    )
    spec = DiscreteTradeSpec(
        entry_ts=activation_ts,
        entry_px=entry,
        direction=direction,
        sl_px=stop_loss,
        tp_levels=[take_profit],
        tp_allocations=[1.0],
        notional_usd=1000.0,
    )
    res = run_discrete_backtest(candles, [spec], cfg)
    if not res.trades:
        return (None, None)
    last_trade = res.trades[-1]
    # Normalise indexed TP labels: backtest emits "tp1"/"tp2"/"tp3" while
    # the reference walker emits "tp" (we only test one TP per scenario).
    raw = last_trade.exit_reason or ""
    normalised = "tp" if raw.startswith("tp") else raw
    return (normalised, last_trade.exit_at)


def test_engine_parity_long_then_sl(base_ts):
    """Real backtest engine must agree with reference walker on a
    long-stops-out scenario."""
    candles = [
        _candle(_ts(base_ts, 1), 102, 102, 99, 100),    # activation at minute 1
        _candle(_ts(base_ts, 2), 100, 102, 94, 96),     # SL=95 wicked
    ]
    sig = dict(direction="long", entry=100.0, stop_loss=95.0, take_profit=110.0, since=base_ts)
    ref = reference_walk(candles, **sig)
    assert ref.exit_kind == "sl"
    eng_reason, eng_exit_ts = _run_backtest_get_exit(
        candles, direction=sig["direction"], entry=sig["entry"],
        stop_loss=sig["stop_loss"], take_profit=sig["take_profit"],
        activation_ts=ref.activation_ts,
    )
    assert eng_reason == "sl"
    assert eng_exit_ts == ref.exit_ts


def test_engine_parity_long_then_tp(base_ts):
    candles = [
        _candle(_ts(base_ts, 1), 102, 102, 99, 100),    # activation
        _candle(_ts(base_ts, 2), 100, 112, 99, 108),    # TP=110 wicked
    ]
    sig = dict(direction="long", entry=100.0, stop_loss=95.0, take_profit=110.0, since=base_ts)
    ref = reference_walk(candles, **sig)
    assert ref.exit_kind == "tp"
    eng_reason, eng_exit_ts = _run_backtest_get_exit(
        candles, direction=sig["direction"], entry=sig["entry"],
        stop_loss=sig["stop_loss"], take_profit=sig["take_profit"],
        activation_ts=ref.activation_ts,
    )
    assert eng_reason == "tp"
    assert eng_exit_ts == ref.exit_ts


def test_engine_parity_short_then_sl(base_ts):
    candles = [
        _candle(_ts(base_ts, 1), 99, 101, 99, 100),     # activation (short entry=100)
        _candle(_ts(base_ts, 2), 100, 106, 99, 104),    # SL=105 wicked
    ]
    sig = dict(direction="short", entry=100.0, stop_loss=105.0, take_profit=90.0, since=base_ts)
    ref = reference_walk(candles, **sig)
    assert ref.exit_kind == "sl"
    eng_reason, eng_exit_ts = _run_backtest_get_exit(
        candles, direction=sig["direction"], entry=sig["entry"],
        stop_loss=sig["stop_loss"], take_profit=sig["take_profit"],
        activation_ts=ref.activation_ts,
    )
    assert eng_reason == "sl"
    assert eng_exit_ts == ref.exit_ts


def test_engine_parity_same_candle_sl_wins(base_ts):
    """The critical ambiguity case: SL and TP both hit in one candle.
    Backtest engine checks SL block (lines 148-201) BEFORE TP block
    (lines 204+) within each candle, matching the reference walker."""
    candles = [
        _candle(_ts(base_ts, 1), 102, 102, 99, 100),    # activation
        _candle(_ts(base_ts, 2), 100, 111, 94, 102),    # both hit
    ]
    sig = dict(direction="long", entry=100.0, stop_loss=95.0, take_profit=110.0, since=base_ts)
    ref = reference_walk(candles, **sig)
    assert ref.exit_kind == "sl"
    eng_reason, eng_exit_ts = _run_backtest_get_exit(
        candles, direction=sig["direction"], entry=sig["entry"],
        stop_loss=sig["stop_loss"], take_profit=sig["take_profit"],
        activation_ts=ref.activation_ts,
    )
    assert eng_reason == "sl"
    assert eng_exit_ts == ref.exit_ts
